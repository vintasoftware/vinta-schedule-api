"""Shared external attendees: one ``ExternalAttendee`` per client identifier.

An external attendee sent with a ``(system, identifier)`` pair is linked to the stored
attendee holding that pair, and created only when none does. These tests drive the
real event sub-service and cover:

- repeated bookings reuse one attendee, each event with its own attendance;
- two bookings racing to create the same new attendee end on one row;
- the shared row's email, name and identifiers are never rewritten by one event, even
  one that names it by id; a row only one event holds is still edited through it;
- an identifier-only attendee sent again unchanged needs no attendee permission;
- removing the attendee from one event, or deleting one event, leaves the others alone,
  and removing the last attendance deletes the unused attendee;
- an attendee with no email never reaches a provider payload.
"""

import datetime
from unittest.mock import Mock, patch

import pytest

from calendar_integration.constants import CalendarProvider, EventManagementPermissions
from calendar_integration.exceptions import ExternalAttendeeIdentifierConflictError
from calendar_integration.factories import create_external_client_identifier
from calendar_integration.models import (
    Calendar,
    CalendarEvent,
    CalendarManagementToken,
    EventExternalAttendance,
    ExternalAttendee,
)
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
from calendar_integration.services.external_event_change_request_service import (
    ExternalEventChangeRequestService,
)
from organizations.models import Organization, OrganizationMembership


PATIENT_SYSTEM = "https://vintasoftware.com/fhir/identifier/medplum-patient"


@pytest.fixture
def organization(db):
    return Organization.objects.create(name="Shared Attendee Org", should_sync_rooms=False)


@pytest.fixture
def calendar(db, organization):
    return Calendar.objects.create(
        name="Shared Attendee Calendar",
        description="A test calendar",
        external_id="shared_attendee_cal_1",
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
        token_hash="shared_attendee_token_hash",
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
def event_service(social_account, social_token, mock_google_adapter, calendar):
    facade = CalendarService()
    facade.authenticate(account=social_account.user, organization=calendar.organization)
    return CalendarEventService(
        context=facade._context,
        recurrence_manager=facade._recurrence_manager,
        calendar_cache=facade._calendar_cache,
        host=facade,
    )


def _grant_event_owner_token(event, user, organization):
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"shared_attendee_token_{event.id}",
        organization=organization,
    )
    token.permissions.all().delete()
    for permission_str in DEFAULT_CALENDAR_OWNER_PERMISSIONS:
        token.permissions.create(permission=permission_str, organization_id=organization.id)
    return token


def _adapter_output(external_id: str, day: int) -> CalendarEventAdapterOutputData:
    return CalendarEventAdapterOutputData(
        calendar_external_id="shared_attendee_cal_1",
        external_id=external_id,
        title="Visit",
        description="",
        start_time=datetime.datetime(2025, 6, day, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, day, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendees=[],
        resources=[],
        original_payload={},
        recurrence_rule=None,
    )


def _patient(patient_id: str, *, email: str = "", name: str = "") -> ExternalAttendeeInputData:
    return ExternalAttendeeInputData(
        email=email,
        name=name,
        external_client_identifiers=[
            ExternalClientIdentifierData(system=PATIENT_SYSTEM, identifier=patient_id)
        ],
    )


def _event_input(day: int, *attendees: ExternalAttendeeInputData) -> CalendarEventInputData:
    return CalendarEventInputData(
        title="Visit",
        description="",
        start_time=datetime.datetime(2025, 6, day, 10, 0, tzinfo=datetime.UTC),
        end_time=datetime.datetime(2025, 6, day, 11, 0, tzinfo=datetime.UTC),
        timezone="UTC",
        attendances=[],
        external_attendances=[
            EventExternalAttendanceInputData(external_attendee=attendee) for attendee in attendees
        ],
        resource_allocations=[],
    )


def _book(event_service, calendar, mock_google_adapter, day, *attendees) -> CalendarEvent:
    mock_google_adapter.create_event.return_value = _adapter_output(f"visit-{day}", day)
    return event_service.create_event(calendar.id, _event_input(day, *attendees))


def _stored_identifiers(attendee: ExternalAttendee) -> dict[str, str]:
    return {row.system: row.identifier for row in attendee.external_client_identifiers.all()}


def _attendees(organization) -> list[ExternalAttendee]:
    return list(ExternalAttendee.objects.filter_by_organization(organization.id).order_by("id"))


@pytest.mark.django_db
def test_repeated_bookings_for_one_patient_reuse_one_attendee(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    first = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    second = _book(event_service, calendar, mock_google_adapter, 23, _patient("patient-1"))

    [attendee] = _attendees(organization)
    assert (attendee.email, attendee.name) == ("", "")
    assert _stored_identifiers(attendee) == {PATIENT_SYSTEM: "patient-1"}
    assert [a.external_attendee_fk_id for a in first.external_attendances.all()] == [attendee.pk]
    assert [a.external_attendee_fk_id for a in second.external_attendances.all()] == [attendee.pk]
    assert EventExternalAttendance.objects.filter_by_organization(organization.id).count() == 2


@pytest.mark.django_db
def test_two_entries_for_the_same_patient_make_one_attendance(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    event = _book(
        event_service,
        calendar,
        mock_google_adapter,
        22,
        _patient("patient-1"),
        _patient("patient-1"),
    )

    [attendee] = _attendees(organization)
    assert [a.external_attendee_fk_id for a in event.external_attendances.all()] == [attendee.pk]


@pytest.mark.django_db
def test_shared_attendee_is_not_rewritten_by_a_later_booking(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    _book(
        event_service,
        calendar,
        mock_google_adapter,
        23,
        _patient("patient-1", email="someone@example.com", name="Someone"),
    )

    [attendee] = _attendees(organization)
    assert (attendee.email, attendee.name) == ("", "")
    assert _stored_identifiers(attendee) == {PATIENT_SYSTEM: "patient-1"}


@pytest.mark.django_db
def test_bookings_racing_for_a_new_patient_end_on_one_attendee(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    """The other booking creates the attendee after this one looked for it and found
    nothing. This booking's identifier insert then fails on
    ``extclientid_uniq_system_ident``; it rolls back its own attendee and links the
    other one."""
    winner = ExternalAttendee.objects.create(organization=organization, email="", name="")
    create_external_client_identifier(
        organization=organization,
        identified_object=winner,
        system=PATIENT_SYSTEM,
        identifier="patient-1",
    )
    original_find = CalendarEventService._find_external_attendee_by_identifiers
    lookups = []

    def find_after_the_race(self, identifier_service, identifiers):
        lookups.append(identifiers)
        if len(lookups) == 1:
            return None
        return original_find(self, identifier_service, identifiers)

    with patch.object(
        CalendarEventService, "_find_external_attendee_by_identifiers", find_after_the_race
    ):
        event = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))

    assert len(lookups) == 2
    assert _attendees(organization) == [winner]
    assert [a.external_attendee_fk_id for a in event.external_attendances.all()] == [winner.pk]


@pytest.mark.django_db
def test_identifiers_held_by_two_attendees_are_refused(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    for patient_id in ("patient-1", "patient-2"):
        attendee = ExternalAttendee.objects.create(organization=organization, email="", name="")
        create_external_client_identifier(
            organization=organization,
            identified_object=attendee,
            system=PATIENT_SYSTEM if patient_id == "patient-1" else "https://other.example.com",
            identifier=patient_id,
        )
    events_before = CalendarEvent.objects.filter_by_organization(organization.id).count()
    both = ExternalAttendeeInputData(
        email="",
        external_client_identifiers=[
            ExternalClientIdentifierData(system=PATIENT_SYSTEM, identifier="patient-1"),
            ExternalClientIdentifierData(
                system="https://other.example.com", identifier="patient-2"
            ),
        ],
    )

    with pytest.raises(ExternalAttendeeIdentifierConflictError):
        _book(event_service, calendar, mock_google_adapter, 22, both)

    assert CalendarEvent.objects.filter_by_organization(organization.id).count() == events_before


@pytest.mark.django_db
def test_update_without_id_links_the_shared_attendee(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    first = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    second = _book(event_service, calendar, mock_google_adapter, 23)
    _grant_event_owner_token(second, social_account.user, organization)
    mock_google_adapter.update_event.return_value = _adapter_output("visit-23", 23)

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, second.id, _event_input(23, _patient("patient-1")))

    [attendee] = _attendees(organization)
    assert [a.external_attendee_fk_id for a in first.external_attendances.all()] == [attendee.pk]
    assert [a.external_attendee_fk_id for a in second.external_attendances.all()] == [attendee.pk]


def _grant_event_token(event, user, organization, permissions) -> None:
    token = CalendarManagementToken.objects.create(
        event_fk=event,
        membership_user_id=user.id,
        token_hash=f"shared_attendee_limited_token_{event.id}",
        organization=organization,
    )
    token.permissions.all().delete()
    for permission_str in permissions:
        token.permissions.create(permission=permission_str, organization_id=organization.id)


@pytest.mark.django_db
def test_shared_attendee_is_not_rewritten_through_its_id(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    """An event that names a shared attendee by id keeps it, but cannot change the
    email, name or identifiers the other events see."""
    first = _book(
        event_service,
        calendar,
        mock_google_adapter,
        22,
        _patient("patient-1", email="pat@example.com", name="Pat"),
    )
    second = _book(event_service, calendar, mock_google_adapter, 23, _patient("patient-1"))
    [attendee] = _attendees(organization)
    _grant_event_owner_token(second, social_account.user, organization)
    mock_google_adapter.update_event.return_value = _adapter_output("visit-23", 23)

    edit = ExternalAttendeeInputData(
        id=attendee.pk,
        email="other@example.com",
        name="Typo",
        external_client_identifiers=[
            ExternalClientIdentifierData(system=PATIENT_SYSTEM, identifier="patient-2")
        ],
    )
    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, second.id, _event_input(23, edit))

    attendee.refresh_from_db()
    assert (attendee.email, attendee.name) == ("pat@example.com", "Pat")
    assert _stored_identifiers(attendee) == {PATIENT_SYSTEM: "patient-1"}
    assert [a.external_attendee_fk_id for a in first.external_attendances.all()] == [attendee.pk]
    assert [a.external_attendee_fk_id for a in second.external_attendances.all()] == [attendee.pk]


@pytest.mark.django_db
def test_attendee_only_one_event_holds_is_edited_through_its_id(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    event = _book(
        event_service,
        calendar,
        mock_google_adapter,
        22,
        _patient("patient-1", email="pat@example.com", name="Typo"),
    )
    [attendee] = _attendees(organization)
    _grant_event_owner_token(event, social_account.user, organization)
    mock_google_adapter.update_event.return_value = _adapter_output("visit-22", 22)

    edit = ExternalAttendeeInputData(id=attendee.pk, email="pat@example.com", name="Pat")
    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, event.id, _event_input(22, edit))

    attendee.refresh_from_db()
    assert (attendee.email, attendee.name) == ("pat@example.com", "Pat")


@pytest.mark.django_db
def test_identifier_only_attendee_sent_again_needs_no_attendee_permission(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    """A token that may change details but not attendees can rename the event while
    re-sending its identifier-only patient: the patient is recognized, not re-added."""
    event = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    [attendee] = _attendees(organization)
    _grant_event_token(
        event, social_account.user, organization, [EventManagementPermissions.UPDATE_DETAILS]
    )
    mock_google_adapter.update_event.return_value = _adapter_output("visit-22", 22)

    renamed = _event_input(22, _patient("patient-1"))
    renamed.title = "Follow-up"
    with django_capture_on_commit_callbacks(execute=True):
        updated = event_service.update_event(calendar.id, event.id, renamed)

    assert updated.title == "Follow-up"
    assert [a.external_attendee_fk_id for a in event.external_attendances.all()] == [attendee.pk]


@pytest.mark.django_db
def test_removing_the_patient_from_one_visit_keeps_the_other_visit(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    first = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    second = _book(event_service, calendar, mock_google_adapter, 23, _patient("patient-1"))
    _grant_event_owner_token(first, social_account.user, organization)
    mock_google_adapter.update_event.return_value = _adapter_output("visit-22", 22)

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, first.id, _event_input(22))

    [attendee] = _attendees(organization)
    assert not first.external_attendances.exists()
    assert [a.external_attendee_fk_id for a in second.external_attendances.all()] == [attendee.pk]
    assert _stored_identifiers(attendee) == {PATIENT_SYSTEM: "patient-1"}


@pytest.mark.django_db
def test_removing_the_last_attendance_deletes_the_unused_attendee(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    event = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    _grant_event_owner_token(event, social_account.user, organization)
    mock_google_adapter.update_event.return_value = _adapter_output("visit-22", 22)

    with django_capture_on_commit_callbacks(execute=True):
        event_service.update_event(calendar.id, event.id, _event_input(22))

    assert _attendees(organization) == []


@pytest.mark.django_db
def test_cancelling_one_visit_keeps_the_patient_on_the_other(
    event_service,
    mock_google_adapter,
    calendar,
    calendar_management_token,
    organization,
    social_account,
    django_capture_on_commit_callbacks,
):
    first = _book(event_service, calendar, mock_google_adapter, 22, _patient("patient-1"))
    second = _book(event_service, calendar, mock_google_adapter, 23, _patient("patient-1"))
    _grant_event_owner_token(first, social_account.user, organization)

    with django_capture_on_commit_callbacks(execute=True):
        event_service.delete_event(calendar.id, first.id)

    [attendee] = _attendees(organization)
    assert (
        not CalendarEvent.objects.filter_by_organization(organization.id)
        .filter(id=first.id)
        .exists()
    )
    assert [a.external_attendee_fk_id for a in second.external_attendances.all()] == [attendee.pk]
    assert _stored_identifiers(attendee) == {PATIENT_SYSTEM: "patient-1"}


@pytest.mark.django_db
def test_attendee_without_email_is_left_out_of_the_provider_payload(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    event = _book(
        event_service,
        calendar,
        mock_google_adapter,
        22,
        _patient("patient-1"),
        ExternalAttendeeInputData(email="guest@example.com", name="Guest"),
    )

    attendees, _resources = event_service._adapter_people_for_event(event)

    assert [(a.email, a.name) for a in attendees] == [("guest@example.com", "Guest")]


@pytest.mark.django_db
def test_attendee_without_email_or_name_has_a_readable_label(organization):
    attendee = ExternalAttendee.objects.create(organization=organization, email="", name="")

    assert str(attendee) == f"External attendee {attendee.pk}"


@pytest.mark.django_db
def test_attendee_without_email_is_left_out_of_the_change_request_undo_payload(
    event_service, mock_google_adapter, calendar, calendar_management_token, organization
):
    event = _book(
        event_service,
        calendar,
        mock_google_adapter,
        22,
        _patient("patient-1"),
        ExternalAttendeeInputData(email="guest@example.com", name="Guest"),
    )

    adapter_input = ExternalEventChangeRequestService(
        audit_service=Mock(), notification_service=Mock()
    )._build_adapter_input(event)

    assert [(a.email, a.name) for a in adapter_input.attendees] == [("guest@example.com", "Guest")]
