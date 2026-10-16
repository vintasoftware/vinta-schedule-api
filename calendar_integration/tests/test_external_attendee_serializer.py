"""``ExternalAttendeeSerializer`` refuses an attendee with neither an email nor an
external client identifier, the same rule the GraphQL inputs apply."""

from calendar_integration.serializers import ExternalAttendeeSerializer
from calendar_integration.services.dataclasses import EXTERNAL_ATTENDEE_CONTACT_REQUIRED_MESSAGE


def test_attendee_with_only_an_identifier_is_valid():
    serializer = ExternalAttendeeSerializer(
        data={
            "name": "Pat",
            "external_client_identifiers": [
                {"system": "https://crm.example.com", "identifier": "patient-1"}
            ],
        }
    )

    assert serializer.is_valid(), serializer.errors


def test_attendee_with_neither_is_refused():
    serializer = ExternalAttendeeSerializer(
        data={"name": "Pat", "email": "", "external_client_identifiers": []}
    )

    assert not serializer.is_valid()
    assert serializer.errors == {"non_field_errors": [EXTERNAL_ATTENDEE_CONTACT_REQUIRED_MESSAGE]}


def test_attendee_sent_by_id_needs_neither():
    """A PATCH may name a stored attendee by id alone."""
    serializer = ExternalAttendeeSerializer(data={"id": 5, "name": "Pat"})

    assert serializer.is_valid(), serializer.errors
