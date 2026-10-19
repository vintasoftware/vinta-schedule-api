"""The meeting value types: ``EventMeetingInputData`` (what a caller asks for) and
``EventMeeting`` (what an event holds, and what each provider write does with it), plus
the database constraint that keeps the stored pair consistent."""

import datetime

from django.db import IntegrityError, transaction

import pytest

from calendar_integration.constants import CalendarProvider, MeetingKind
from calendar_integration.exceptions import InvalidMeetingError
from calendar_integration.models import Calendar, CalendarEvent
from calendar_integration.services.dataclasses import (
    MEETING_URL_NOT_ALLOWED_MESSAGE,
    MEETING_URL_REQUIRED_MESSAGE,
    AdapterMeeting,
    EventMeeting,
    EventMeetingInputData,
)
from organizations.models import Organization


CUSTOM_URL = "https://zoom.us/j/123"
MEET_URL = "https://meet.google.com/abc-defg-hij"

NO_MEETING = EventMeeting()
CUSTOM = EventMeeting(kind=MeetingKind.CUSTOM, url=CUSTOM_URL)
GENERATED_PENDING = EventMeeting(kind=MeetingKind.GENERATED)
GENERATED = EventMeeting(kind=MeetingKind.GENERATED, url=MEET_URL)


@pytest.mark.parametrize(
    ("kind", "url", "message"),
    [
        (MeetingKind.CUSTOM, "", MEETING_URL_REQUIRED_MESSAGE),
        (MeetingKind.CUSTOM, "http://zoom.us/j/123", MEETING_URL_REQUIRED_MESSAGE),
        # 2049 characters: one over the 2048 the column holds.
        (
            MeetingKind.CUSTOM,
            "https://zoom.us/" + "a" * (2049 - len("https://zoom.us/")),
            MEETING_URL_REQUIRED_MESSAGE,
        ),
        (MeetingKind.GENERATED, CUSTOM_URL, MEETING_URL_NOT_ALLOWED_MESSAGE),
        (None, CUSTOM_URL, MEETING_URL_NOT_ALLOWED_MESSAGE),
    ],
    ids=["custom-no-url", "custom-http", "custom-too-long", "generated-url", "removal-url"],
)
def test_a_meeting_input_that_does_not_fit_its_kind_cannot_be_built(kind, url, message):
    with pytest.raises(InvalidMeetingError, match=message):
        EventMeetingInputData(kind=kind, url=url)


@pytest.mark.parametrize(
    ("current", "requested", "expected"),
    [
        (CUSTOM, None, CUSTOM),
        (GENERATED, EventMeetingInputData(kind=MeetingKind.GENERATED), GENERATED),
        (CUSTOM, EventMeetingInputData(kind=MeetingKind.GENERATED), GENERATED_PENDING),
        (GENERATED, EventMeetingInputData(kind=None), NO_MEETING),
        (
            GENERATED,
            EventMeetingInputData(kind=MeetingKind.CUSTOM, url=CUSTOM_URL),
            CUSTOM,
        ),
    ],
    ids=["omitted-keeps", "same-generated-keeps-link", "to-generated", "removed", "to-custom"],
)
def test_after_applies_an_update(current, requested, expected):
    assert current.after(requested) == expected


@pytest.mark.parametrize(
    ("meeting", "previous", "expected"),
    [
        (CUSTOM, None, AdapterMeeting(location=CUSTOM_URL)),
        (GENERATED_PENDING, None, AdapterMeeting(conference="create")),
        (NO_MEETING, None, AdapterMeeting()),
        (GENERATED, GENERATED, AdapterMeeting()),
        (CUSTOM, CUSTOM, AdapterMeeting(location=CUSTOM_URL)),
        (GENERATED_PENDING, CUSTOM, AdapterMeeting(conference="create")),
        (CUSTOM, GENERATED, AdapterMeeting(location=CUSTOM_URL, conference="remove")),
        (NO_MEETING, GENERATED, AdapterMeeting(conference="remove")),
    ],
    ids=[
        "new-custom",
        "new-generated",
        "new-none",
        "kept-generated",
        "kept-custom",
        "custom-to-generated",
        "generated-to-custom",
        "generated-removed",
    ],
)
def test_adapter_request_touches_the_conference_only_when_the_kind_changes(
    meeting, previous, expected
):
    assert meeting.adapter_request(previous=previous) == expected


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("kind", "url", "allowed"),
    [
        (None, "", True),
        (MeetingKind.CUSTOM, CUSTOM_URL, True),
        (MeetingKind.GENERATED, "", True),
        (MeetingKind.GENERATED, MEET_URL, True),
        (None, CUSTOM_URL, False),
        (MeetingKind.CUSTOM, "", False),
    ],
    ids=["none", "custom", "generated-pending", "generated", "url-without-kind", "custom-no-url"],
)
def test_the_database_refuses_an_inconsistent_meeting(kind, url, allowed):
    organization = Organization.objects.create(name="Meeting Org", should_sync_rooms=False)
    calendar = Calendar.objects.create(
        organization=organization,
        name="Meeting Calendar",
        external_id="meeting_cal",
        provider=CalendarProvider.INTERNAL,
    )

    def create() -> None:
        CalendarEvent.objects.create(
            organization=organization,
            calendar_fk=calendar,
            title="Visit",
            start_time_tz_unaware=datetime.datetime(2025, 6, 22, 10, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 22, 11, 0),
            timezone="UTC",
            meeting_kind=kind,
            meeting_url=url,
        )

    if allowed:
        create()
    else:
        with pytest.raises(IntegrityError), transaction.atomic():
            create()
