"""Booking a patient known only by a client identifier through the public API.

An organization-wide token books an appointment type for an external attendee that has
no email and no name, only a ``(system, identifier)`` pair naming the patient in the
client's system. The event carries its own pair pointing at the client's record of the
visit. Covered here, over the real ``/graphql/`` stack:

- the typed create stores no email or name and returns both pairs;
- rescheduling keeps the attendee and its pair;
- an attendee with neither an email nor an identifier is refused;
- ``updateCalendarEvent`` keeps a patient re-sent with its pair and no email, and takes
  two such patients in one payload without calling their empty emails duplicates.

That a second booking reuses the same attendee is covered by the event service tests.
"""

import datetime
import uuid

import pytest
from model_bakery import baker

from calendar_integration.constants import CalendarProvider, CalendarType
from calendar_integration.models import (
    AppointmentType,
    AppointmentTypeSlot,
    AppointmentTypeSlotMembership,
    AvailableTime,
    Calendar,
    CalendarEvent,
)
from organizations.models import Organization
from public_api.constants import PublicAPIResources
from public_api.tests.helpers import GraphQLPost, make_token_post


pytestmark = pytest.mark.django_db

PATIENT_SYSTEM = "https://vintasoftware.com/fhir/identifier/medplum-patient"
APPOINTMENT_SYSTEM = "https://vintasoftware.com/fhir/identifier/medplum-appointment"

EVENT_FIELDS = """
    id
    externalClientIdentifiers { system identifier }
    externalAttendances {
        externalAttendee { id email name externalClientIdentifiers { system identifier } }
    }
"""

CREATE_APPOINTMENT_TYPE_EVENT_MUTATION = f"""
mutation CreateAppointmentTypeEvent($input: AppointmentTypeEventInput!) {{
    createAppointmentTypeEvent(input: $input) {{
        success
        errorMessage
        event {{ {EVENT_FIELDS} }}
    }}
}}
"""

RESCHEDULE_APPOINTMENT_TYPE_EVENT_MUTATION = f"""
mutation RescheduleAppointmentTypeEvent($input: RescheduleAppointmentTypeEventInput!) {{
    rescheduleAppointmentTypeEvent(input: $input) {{ {EVENT_FIELDS} }}
}}
"""

UPDATE_CALENDAR_EVENT_MUTATION = f"""
mutation UpdateCalendarEvent($input: UpdateCalendarEventInput!) {{
    updateCalendarEvent(input: $input) {{ {EVENT_FIELDS} }}
}}
"""


@pytest.fixture
def organization() -> Organization:
    return baker.make(Organization, name="Public API Org")


@pytest.fixture
def appointment_type_with_slot(
    organization: Organization,
) -> tuple[AppointmentType, AppointmentTypeSlot]:
    """A private appointment type whose one slot books an internal calendar open on
    2030-06-01 from 09:00 to 17:00 UTC."""
    calendar = baker.make(
        Calendar,
        organization=organization,
        name="Provider Calendar",
        external_id=f"booking-{uuid.uuid4().hex[:8]}",
        provider=CalendarProvider.INTERNAL,
        calendar_type=CalendarType.PERSONAL,
        manage_available_windows=True,
        accepts_public_scheduling=False,
    )
    AvailableTime.objects.create(
        organization=organization,
        calendar=calendar,
        start_time_tz_unaware=datetime.datetime(2030, 6, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2030, 6, 1, 17, 0),
        timezone="UTC",
    )
    appointment_type = baker.make(
        AppointmentType,
        organization=organization,
        name="Private Visit",
        accepts_public_scheduling=False,
    )
    slot = AppointmentTypeSlot.objects.create(
        organization=organization,
        appointment_type=appointment_type,
        name="Provider",
        order=0,
        required_count=1,
    )
    AppointmentTypeSlotMembership.objects.create(
        organization=organization, slot=slot, calendar=calendar
    )
    return appointment_type, slot


@pytest.fixture
def org_wide_post(organization: Organization) -> GraphQLPost:
    """post() for an organization-wide token holding APPOINTMENT_TYPE and CALENDAR_EVENT."""
    return make_token_post(
        organization, [PublicAPIResources.APPOINTMENT_TYPE, PublicAPIResources.CALENDAR_EVENT]
    )


def _patient(patient_id: str, **fields) -> dict:
    return {
        "externalClientIdentifiers": [{"system": PATIENT_SYSTEM, "identifier": patient_id}],
        **fields,
    }


def _book_variables(
    organization, appointment_type_with_slot, *, hour: int, attendee: dict, appointment_id=None
) -> dict:
    appointment_type, slot = appointment_type_with_slot
    calendar_id = slot.memberships.get().calendar_fk_id
    variables = {
        "input": {
            "organizationId": organization.id,
            "appointmentTypeId": appointment_type.id,
            "title": "Visit",
            "description": "",
            "startTime": f"2030-06-01T{hour:02d}:00:00",
            "endTime": f"2030-06-01T{hour:02d}:30:00",
            "timezone": "UTC",
            "slotSelections": [{"slotId": slot.id, "calendarIds": [calendar_id]}],
            "externalAttendances": [{"externalAttendee": attendee}],
        }
    }
    if appointment_id is not None:
        variables["input"]["externalClientIdentifiers"] = [
            {"system": APPOINTMENT_SYSTEM, "identifier": appointment_id}
        ]
    return variables


def _book(post, organization, appointment_type_with_slot, **kwargs) -> dict:
    return post(
        CREATE_APPOINTMENT_TYPE_EVENT_MUTATION,
        _book_variables(organization, appointment_type_with_slot, **kwargs),
    )["data"]["createAppointmentTypeEvent"]


def test_typed_create_for_an_identifier_only_patient(
    org_wide_post, organization, appointment_type_with_slot
):
    result = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=10,
        attendee=_patient("patient-1"),
        appointment_id="appointment-1",
    )

    assert result["errorMessage"] is None
    event = result["event"]
    assert event["externalClientIdentifiers"] == [
        {"system": APPOINTMENT_SYSTEM, "identifier": "appointment-1"}
    ]
    [attendance] = event["externalAttendances"]
    assert attendance["externalAttendee"]["email"] == ""
    assert attendance["externalAttendee"]["name"] == ""
    assert attendance["externalAttendee"]["externalClientIdentifiers"] == [
        {"system": PATIENT_SYSTEM, "identifier": "patient-1"}
    ]


def test_reschedule_keeps_the_patient_and_its_identifier(
    org_wide_post, organization, appointment_type_with_slot
):
    booked = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=10,
        attendee=_patient("patient-1"),
    )
    booked_attendee = booked["event"]["externalAttendances"][0]["externalAttendee"]

    rescheduled = org_wide_post(
        RESCHEDULE_APPOINTMENT_TYPE_EVENT_MUTATION,
        {
            "input": {
                "organizationId": organization.id,
                "eventId": int(booked["event"]["id"]),
                "startTime": "2030-06-01T14:00:00",
                "endTime": "2030-06-01T14:30:00",
                "timezone": "UTC",
            }
        },
    )

    assert rescheduled.get("errors") is None
    [attendance] = rescheduled["data"]["rescheduleAppointmentTypeEvent"]["externalAttendances"]
    assert attendance["externalAttendee"] == booked_attendee


def test_attendee_with_neither_email_nor_identifier_is_refused(
    org_wide_post, organization, appointment_type_with_slot
):
    result = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=10,
        attendee={"name": "Nobody"},
    )

    assert result == {
        "success": False,
        "errorMessage": (
            "Each external attendee needs an email or at least one external client identifier."
        ),
        "event": None,
    }
    assert not CalendarEvent.objects.filter_by_organization(organization.id).exists()


def test_update_keeps_patients_resent_without_email(
    org_wide_post, organization, appointment_type_with_slot
):
    booked = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=10,
        attendee=_patient("patient-1"),
    )
    patient_attendee = booked["event"]["externalAttendances"][0]["externalAttendee"]

    updated = org_wide_post(
        UPDATE_CALENDAR_EVENT_MUTATION,
        {
            "input": {
                "organizationId": organization.id,
                "eventId": int(booked["event"]["id"]),
                "externalAttendees": [
                    _patient("patient-1"),
                    _patient("patient-2"),
                    {"email": "guest@example.com", "name": "Guest"},
                ],
            }
        },
    )

    assert updated.get("errors") is None
    attendees = [
        a["externalAttendee"] for a in updated["data"]["updateCalendarEvent"]["externalAttendances"]
    ]
    assert patient_attendee in attendees
    assert sorted(
        (a["email"], [i["identifier"] for i in a["externalClientIdentifiers"]]) for a in attendees
    ) == [("", ["patient-1"]), ("", ["patient-2"]), ("guest@example.com", [])]
