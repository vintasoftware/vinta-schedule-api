"""Booking a patient known only by a client identifier through the public API.

An organization-wide token books an appointment type for an external attendee that has
no email and no name, only a ``(system, identifier)`` pair naming the patient in the
client's system. The event carries its own pair pointing at the client's record of the
visit. Covered here, over the real ``/graphql/`` stack:

- the typed create stores no email or name, returns both pairs, and sends the event's
  pair in the ``calendar_event_created`` webhook;
- rescheduling keeps the attendee and its pair;
- an attendee with neither an email nor an identifier is refused;
- an event pair already used by another event is refused;
- ``updateCalendarEvent`` keeps a patient re-sent with its pair and no email, and takes
  two such patients in one payload without calling their empty emails duplicates.

That a second booking reuses the same attendee is covered by the event service tests.
"""

from unittest.mock import patch

import pytest

from calendar_integration.models import CalendarEvent
from webhooks.constants import WebhookEventType


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
    org_wide_post, organization, appointment_type_with_slot, django_capture_on_commit_callbacks
):
    sent = []

    def record_send_event(self, *, organization, event_type, payload):
        sent.append((event_type, payload))
        return []

    with (
        patch("webhooks.services.webhook_service.WebhookService.send_event", record_send_event),
        django_capture_on_commit_callbacks(execute=True),
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
    [created_payload] = [
        payload
        for event_type, payload in sent
        if event_type == WebhookEventType.CALENDAR_EVENT_CREATED
    ]
    assert created_payload["id"] == int(event["id"])
    assert created_payload["external_client_identifiers"] == [
        {"system": APPOINTMENT_SYSTEM, "identifier": "appointment-1"}
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


def test_event_identifier_already_used_by_another_event_is_refused(
    org_wide_post, organization, appointment_type_with_slot
):
    _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=10,
        attendee=_patient("patient-1"),
        appointment_id="appointment-1",
    )

    result = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=11,
        attendee=_patient("patient-2"),
        appointment_id="appointment-1",
    )

    assert result == {
        "success": False,
        "errorMessage": "That (system, identifier) pair is already in use by another record.",
        "event": None,
    }
    assert CalendarEvent.objects.filter_by_organization(organization.id).count() == 1


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


def test_update_refuses_an_attendee_with_neither_email_nor_identifier(
    org_wide_post, organization, appointment_type_with_slot
):
    booked = _book(
        org_wide_post,
        organization,
        appointment_type_with_slot,
        hour=10,
        attendee=_patient("patient-1"),
    )

    updated = org_wide_post(
        UPDATE_CALENDAR_EVENT_MUTATION,
        {
            "input": {
                "organizationId": organization.id,
                "eventId": int(booked["event"]["id"]),
                "externalAttendees": [{"name": "Nobody"}],
            }
        },
    )

    assert [e["message"] for e in updated["errors"]] == [
        "Each external attendee needs an email or at least one external client identifier."
    ]
