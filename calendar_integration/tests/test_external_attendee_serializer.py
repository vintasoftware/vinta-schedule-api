"""``ExternalAttendeeSerializer`` refuses an attendee with neither an email nor an
external client identifier, the same rule the GraphQL inputs apply."""

import pytest

from calendar_integration.serializers import ExternalAttendeeSerializer
from calendar_integration.services.dataclasses import EXTERNAL_ATTENDEE_CONTACT_REQUIRED_MESSAGE


@pytest.mark.parametrize(
    "data",
    [
        {"email": "pat@example.com"},
        {
            "external_client_identifiers": [
                {"system": "https://crm.example.com", "identifier": "patient-1"}
            ]
        },
    ],
    ids=["email", "identifier"],
)
def test_attendee_with_an_email_or_an_identifier_is_valid(data):
    serializer = ExternalAttendeeSerializer(data={"name": "Pat", **data})

    assert serializer.is_valid(), serializer.errors


@pytest.mark.parametrize(
    "data",
    [{}, {"email": ""}, {"external_client_identifiers": []}],
    ids=["nothing", "blank-email", "no-identifiers"],
)
def test_attendee_with_neither_is_refused(data):
    serializer = ExternalAttendeeSerializer(data={"name": "Pat", **data})

    assert not serializer.is_valid()
    assert serializer.errors == {"non_field_errors": [EXTERNAL_ATTENDEE_CONTACT_REQUIRED_MESSAGE]}
