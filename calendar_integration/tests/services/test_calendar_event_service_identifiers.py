"""Integration tests for identifier writes threaded through ``CalendarEventService``.

Drives the real event sub-service (built from a facade whose services -- including
``ExternalClientIdentifierService`` -- are injected via the DI container, exactly as
production wiring does), and covers:

- ``create_event`` persists identifiers for the event and its external attendees.
- ``update_event`` persists identifiers for the event and its external attendees.
- ``update_event`` omitting the field leaves stored identifiers untouched (the tri-state
  ``None`` no-op) -- proven against a seeded non-empty stored set, so a broken
  implementation that always clears would fail this test.
- ``update_event`` sending ``[]`` clears the stored set.
- An external attendee deleted-and-recreated during reconciliation (an incoming payload
  that omits its id) keeps the identifiers supplied for it -- landed on the NEW row, not
  silently dropped with the old one.
- A duplicate ``(system, identifier)`` rolls the entire event write back -- no partial
  event, no partial attendee.
- The audit diff carries the identifier change under ``external_client_identifiers`` and
  omits the key entirely when the set did not change.
"""

import datetime
from unittest.mock import Mock, patch

from django.db import IntegrityError

import pytest
from allauth.socialaccount.models import SocialAccount, SocialToken
from model_bakery import baker

from calendar_integration.constants import CalendarProvider
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import Calendar, CalendarEvent, CalendarManagementToken
from calendar_integration.services.calendar_event_service import CalendarEventService
from calendar_integration.services.calendar_permission_service import (
    DEFAULT_CALENDAR_OWNER_PERMISSIONS,
)
from calendar_integration.services.calendar_service import CalendarService
from calendar_integration.services.dataclasses import (
    CalendarEventAdapterOutputData,
    CalendarEventInputData,
    EventExternalAttendanceInputData,
    ExternalAttendeeInputData,
    ExternalClientIdentifierData,
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
    return Organization.objects.create(name="Identifier Event Org", should_sync_rooms=False)


@pytest.fixture
def social_account(db):
    user = User.objects.create_user(email="identifier-event@example.com", password="testpass123")
    Profile.objects.create(user=user)
    return SocialAccount.objects.create(user=user, provider=CalendarProvider.GOOGLE, uid="77777")


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
        name="Identifier Event Calendar",
        description="A test calendar",
        external_id="evt_ident_cal_1",
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
        token_hash="evt_ident_token_hash",
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
        token_hash=f"evt_ident_token_{event.id}",
        organization=organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(
            permission=permission_str,
            organization_id=organization.id,
        )
    return token


def _adapter_output(external_id: str):
    return CalendarEventAdapterOutputData(
        calendar_external_id="evt_ident_cal_1",
        external_id=external_id,
        title="Identifier Event",
        description="An event with identifiers",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=None,
    )


def _base_event_input(**overrides):
    defaults = dict(
        title="Identifier Event",
        description="An event with identifiers",
        start_time=datetime.datetime(2025, 6, 22, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, 22, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[],
        resource_allocations=[],
    )
    defaults.update(overrides)
    return CalendarEventInputData(**defaults)


def _stored_identifiers(target) -> dict[str, str]:
    return {row.system: row.identifier for row in target.external_client_identifiers.all()}


def _payloads(mock_task) -> list[dict]:
    return [call.args[0] for call in mock_task.delay.call_args_list]


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_create_event_persists_event_and_attendee_identifiers(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("evt-create-ident")

    event_input = _base_event_input(
        external_client_identifiers=[
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1")
        ],
        external_attendances=[
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email="ext@example.com",
                    name="Ext Attendee",
                    external_client_identifiers=[
                        ExternalClientIdentifierData(
                            system="https://crm.example.com", identifier="contact-1"
                        )
                    ],
                )
            )
        ],
    )

    with django_capture_on_commit_callbacks(execute=True):
        event = event_service.create_event(calendar.id, event_input)

    assert _stored_identifiers(event) == {"https://crm.example.com": "deal-1"}

    attendance = event.external_attendances.get()
    assert _stored_identifiers(attendance.external_attendee) == {
        "https://crm.example.com": "contact-1"
    }


@pytest.mark.django_db
def test_create_event_without_identifiers_creates_no_rows(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    django_capture_on_commit_callbacks,
):
    """Regression: a caller that never mentions identifiers writes none."""
    mock_google_adapter.create_event.return_value = _adapter_output("evt-create-noident")

    event_input = _base_event_input()

    with django_capture_on_commit_callbacks(execute=True):
        event = event_service.create_event(calendar.id, event_input)

    assert _stored_identifiers(event) == {}


# ---------------------------------------------------------------------------
# update_event: persist / omit / clear
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_persists_identifiers(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("evt-update-ident")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-update-ident")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(calendar.id, _base_event_input())
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    updated_input = _base_event_input(
        external_client_identifiers=[
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-9")
        ]
    )

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, created.id, updated_input)

    created.refresh_from_db()
    assert _stored_identifiers(created) == {"https://crm.example.com": "deal-9"}


@pytest.mark.django_db
def test_update_event_omitting_identifiers_leaves_stored_untouched(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    """Proves the tri-state ``None`` no-op: seeded identifiers survive an update call
    that never mentions the field. Capable of failing -- a broken implementation that
    treats an omitted field as "clear" (e.g. a stray ``[]`` default) would empty the
    stored set here."""
    mock_google_adapter.create_event.return_value = _adapter_output("evt-update-omit")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-update-omit")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(
            calendar.id,
            _base_event_input(
                external_client_identifiers=[
                    ExternalClientIdentifierData(
                        system="https://crm.example.com", identifier="deal-1"
                    )
                ]
            ),
        )
    _grant_event_owner_token(created, social_account.user, calendar.organization)
    assert _stored_identifiers(created) == {"https://crm.example.com": "deal-1"}

    # `_base_event_input` leaves `external_client_identifiers` at its dataclass default
    # (`None`) -- omitted.
    updated_input = _base_event_input(title="Renamed, identifiers untouched")

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, created.id, updated_input)

    created.refresh_from_db()
    assert created.title == "Renamed, identifiers untouched"
    assert _stored_identifiers(created) == {"https://crm.example.com": "deal-1"}


@pytest.mark.django_db
def test_update_event_empty_list_clears_identifiers(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("evt-update-clear")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-update-clear")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(
            calendar.id,
            _base_event_input(
                external_client_identifiers=[
                    ExternalClientIdentifierData(
                        system="https://crm.example.com", identifier="deal-1"
                    )
                ]
            ),
        )
    _grant_event_owner_token(created, social_account.user, calendar.organization)
    assert _stored_identifiers(created) == {"https://crm.example.com": "deal-1"}

    updated_input = _base_event_input(external_client_identifiers=[])

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, created.id, updated_input)

    created.refresh_from_db()
    assert _stored_identifiers(created) == {}


# ---------------------------------------------------------------------------
# Attendee delete-and-recreate reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_attendee_deleted_and_recreated_keeps_new_identifiers(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    """An incoming attendee whose id is omitted is deleted-and-recreated by the
    existing reconciliation loop. Identifiers supplied for it must land on the NEW row
    -- the old row (and its identifiers) is gone by cascade."""
    mock_google_adapter.create_event.return_value = _adapter_output("evt-attendee-recreate")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-attendee-recreate")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(
            calendar.id,
            _base_event_input(
                external_attendances=[
                    EventExternalAttendanceInputData(
                        external_attendee=ExternalAttendeeInputData(
                            email="ext@example.com",
                            name="Ext Attendee",
                            external_client_identifiers=[
                                ExternalClientIdentifierData(
                                    system="https://crm.example.com", identifier="old-contact"
                                )
                            ],
                        )
                    )
                ]
            ),
        )
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    original_attendance = created.external_attendances.get()
    original_attendee_id = original_attendance.external_attendee_fk_id

    # Re-send the same logical attendee WITHOUT its id -- the reconciliation loop
    # cannot match it to the existing row, so it deletes the old one and creates a new
    # one. New identifiers are supplied for it.
    updated_input = _base_event_input(
        external_attendances=[
            EventExternalAttendanceInputData(
                external_attendee=ExternalAttendeeInputData(
                    email="ext@example.com",
                    name="Ext Attendee",
                    id=None,
                    external_client_identifiers=[
                        ExternalClientIdentifierData(
                            system="https://crm.example.com", identifier="new-contact"
                        )
                    ],
                )
            )
        ]
    )

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, created.id, updated_input)

    from calendar_integration.models import ExternalAttendee

    assert (
        not ExternalAttendee.objects.filter_by_organization(calendar.organization_id)
        .filter(pk=original_attendee_id)
        .exists()
    )

    new_attendance = created.external_attendances.get()
    assert new_attendance.external_attendee_fk_id != original_attendee_id
    assert _stored_identifiers(new_attendance.external_attendee) == {
        "https://crm.example.com": "new-contact"
    }


# ---------------------------------------------------------------------------
# Duplicate (system, identifier) rollback
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_duplicate_identifier_on_create_rolls_back_whole_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
):
    other_event = baker.make(
        CalendarEvent,
        organization=calendar.organization,
        calendar=calendar,
        title="Other Event",
        external_id="evt-other-1",
        start_time_tz_unaware=datetime.datetime(2025, 6, 20, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 20, 10, 0, 0),
        timezone="UTC",
    )
    create_external_client_identifier(
        organization=calendar.organization,
        identified_object=other_event,
        system="https://crm.example.com",
        identifier="dup-1",
    )

    mock_google_adapter.create_event.return_value = _adapter_output("evt-create-dup")

    events_before = CalendarEvent.objects.filter_by_organization(calendar.organization_id).count()

    event_input = _base_event_input(
        external_client_identifiers=[
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="dup-1")
        ]
    )

    with pytest.raises(IntegrityError):
        event_service.create_event(calendar.id, event_input)

    events_after = CalendarEvent.objects.filter_by_organization(calendar.organization_id).count()
    assert events_after == events_before


@pytest.mark.django_db
def test_duplicate_identifier_on_update_rolls_back_whole_event(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    other_event = baker.make(
        CalendarEvent,
        organization=calendar.organization,
        calendar=calendar,
        title="Other Event",
        external_id="evt-other-2",
        start_time_tz_unaware=datetime.datetime(2025, 6, 20, 9, 0, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 20, 10, 0, 0),
        timezone="UTC",
    )
    create_external_client_identifier(
        organization=calendar.organization,
        identified_object=other_event,
        system="https://crm.example.com",
        identifier="dup-2",
    )

    mock_google_adapter.create_event.return_value = _adapter_output("evt-update-dup")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-update-dup")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(calendar.id, _base_event_input())
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    updated_input = _base_event_input(
        title="Should not stick",
        external_client_identifiers=[
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="dup-2")
        ],
    )

    with pytest.raises(IntegrityError):
        event_service.update_event(calendar.id, created.id, updated_input)

    created.refresh_from_db()
    assert created.title == "Identifier Event"
    assert _stored_identifiers(created) == {}


# ---------------------------------------------------------------------------
# Audit diff
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_event_audit_diff_includes_identifier_change(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    mock_google_adapter.create_event.return_value = _adapter_output("evt-audit-ident")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-audit-ident")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(calendar.id, _base_event_input())
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    updated_input = _base_event_input(
        external_client_identifiers=[
            ExternalClientIdentifierData(system="https://crm.example.com", identifier="deal-1")
        ]
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
    diff = event_payloads[0]["diff"]
    assert diff is not None
    assert diff["external_client_identifiers"] == {
        "old": [],
        "new": [{"system": "https://crm.example.com", "identifier": "deal-1"}],
    }


@pytest.mark.django_db
def test_update_event_audit_diff_omits_identifier_key_when_unchanged(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    """When the caller omits identifiers (and nothing else changes), no audit record is
    even emitted for a diff-less update -- and when something else DOES change, the
    ``external_client_identifiers`` key must be absent."""
    mock_google_adapter.create_event.return_value = _adapter_output("evt-audit-noident")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-audit-noident")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(
            calendar.id,
            _base_event_input(
                external_client_identifiers=[
                    ExternalClientIdentifierData(
                        system="https://crm.example.com", identifier="deal-1"
                    )
                ]
            ),
        )
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    # Title changes, identifiers omitted (None) -- the scalar diff still fires, but it
    # must not carry an `external_client_identifiers` key.
    updated_input = _base_event_input(title="New Title")

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.update_event(calendar.id, created.id, updated_input)

    event_payloads = [
        p
        for p in _payloads(mock_task)
        if p["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    ]
    assert len(event_payloads) == 1
    diff = event_payloads[0]["diff"]
    assert diff is not None
    assert diff["title"] == {"old": "Identifier Event", "new": "New Title"}
    assert "external_client_identifiers" not in diff


@pytest.mark.django_db
def test_update_event_audit_has_no_diff_when_nothing_changes(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    social_account,
    django_capture_on_commit_callbacks,
):
    """Full byte-identical regression: re-sending the exact same input (identifiers
    omitted) produces a diff-less audit record, exactly as it did before this phase."""
    mock_google_adapter.create_event.return_value = _adapter_output("evt-audit-identical")
    mock_google_adapter.update_event.return_value = _adapter_output("evt-audit-identical")

    with django_capture_on_commit_callbacks(execute=True):
        created = event_service.create_event(calendar.id, _base_event_input())
    _grant_event_owner_token(created, social_account.user, calendar.organization)

    with patch("audit.services.persist_audit_record") as mock_task:
        with django_capture_on_commit_callbacks(execute=True):
            event_service.update_event(calendar.id, created.id, _base_event_input())

    event_payloads = [
        p
        for p in _payloads(mock_task)
        if p["subject"]["subject_type"] == "calendar_integration.CalendarEvent"
    ]
    assert len(event_payloads) == 1
    assert event_payloads[0]["diff"] is None
