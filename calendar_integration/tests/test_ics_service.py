"""Unit tests for the CalendarEventICSService.

Tests verify that build_ics produces valid RFC 5545 iCalendar documents that:
- Round-trip through icalendar.Calendar.from_ical for validity
- Contain correct UID (external_id when present, synthetic fallback otherwise)
- Contain correct SUMMARY, DTSTART, DTEND, STATUS, SEQUENCE, DTSTAMP
- Properly escape special characters (commas, semicolons, newlines) in description
"""

import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import icalendar
import pytest
from model_bakery import baker

from calendar_integration.models import CalendarEvent
from calendar_integration.services import CalendarEventICSService


@pytest.mark.django_db
def test_build_ics_basic_event():
    """Test building ICS for a simple event with external_id."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Team Meeting",
        description="Weekly sync",
        external_id="evt-123-external",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        timezone="America/New_York",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    # Verify it's bytes
    assert isinstance(ics_bytes, bytes)

    # Parse back to verify validity
    cal = icalendar.Calendar.from_ical(ics_bytes)
    assert cal is not None

    # Extract the event from the calendar
    events = [c for c in cal.walk("VEVENT")]
    assert len(events) == 1
    vevent = events[0]

    # Verify key properties
    assert vevent.get("uid") == "evt-123-external"
    assert vevent.get("summary") == "Team Meeting"
    assert vevent.get("description") == "Weekly sync"
    assert vevent.get("status") == "CONFIRMED"
    assert vevent.get("dtstamp") is not None
    assert vevent.get("sequence") is not None


@pytest.mark.django_db
def test_build_ics_synthetic_uid():
    """Test that events without external_id get synthetic uid."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Lunch",
        description="",
        external_id="",  # No external ID
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 12, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 13, 0),
        timezone="America/New_York",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    # Verify synthetic UID format
    uid = vevent.get("uid")
    assert uid == f"event-{event.id}@vinta-schedule"


@pytest.mark.django_db
def test_build_ics_with_special_chars_in_description():
    """Test that special characters in description are properly escaped."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    # Description with commas, semicolons, and newlines
    description = "Agenda; review items, decisions\nNext steps, follow-up"

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Planning",
        description=description,
        external_id="evt-456",
        start_time_tz_unaware=datetime.datetime(2025, 6, 22, 14, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 22, 15, 0),
        timezone="Europe/London",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    # Parse back to verify escaping is correct
    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    # The raw ICS must escape the special characters per RFC 5545.
    ics_str = ics_bytes.decode("utf-8")
    assert "\\;" in ics_str
    assert "\\," in ics_str
    assert "\\n" in ics_str

    # After unescaping on parse, the description must round-trip exactly.
    parsed_desc = str(vevent.get("description"))
    assert parsed_desc == description


@pytest.mark.django_db
def test_build_ics_without_description():
    """Test that events without description don't include DESCRIPTION line."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Quick sync",
        description="",  # Empty description
        external_id="evt-789",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 15, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 15, 30),
        timezone="UTC",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    # Empty description should not be included
    description = vevent.get("description")
    assert description is None or str(description).strip() == ""


@pytest.mark.django_db
def test_build_ics_timezone_aware_times():
    """Test that DTSTART/DTEND are timezone-aware."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Pacific Event",
        description="",
        external_id="evt-tz",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 11, 0),
        timezone="America/Los_Angeles",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    # Check raw ICS contains TZID or UTC Z suffix
    ics_str = ics_bytes.decode("utf-8")
    # The event should have timezone info in DTSTART/DTEND
    assert "DTSTART" in ics_str
    assert "DTEND" in ics_str

    # Parse and verify the times are present
    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    dtstart = vevent.get("dtstart")
    dtend = vevent.get("dtend")
    assert dtstart is not None
    assert dtend is not None
    assert dtstart.dt < dtend.dt

    # DTSTART must be timezone-aware (not a floating/naive datetime).
    assert dtstart.dt.tzinfo is not None
    assert dtend.dt.tzinfo is not None

    # The serialized instant must equal the event's timezone-aware start_time.
    # event.start_time is the America/Los_Angeles wall-clock; both refer to the
    # same UTC instant, so comparing absolute instants proves the conversion.
    expected_start = event.start_time
    expected_end = event.end_time
    assert dtstart.dt.astimezone(datetime.UTC) == expected_start.astimezone(datetime.UTC)
    assert dtend.dt.astimezone(datetime.UTC) == expected_end.astimezone(datetime.UTC)

    # The PST/PDT offset must be reflected: 2025-06-21 is during PDT (UTC-7).
    la_start = expected_start.astimezone(ZoneInfo("America/Los_Angeles"))
    assert la_start.utcoffset() == datetime.timedelta(hours=-7)


@pytest.mark.django_db
def test_build_ics_sequence_from_modified():
    """Test that SEQUENCE is derived from modified timestamp."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Updated Event",
        description="",
        external_id="evt-seq",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        timezone="UTC",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    # Sequence should be the integer derived from the modified timestamp
    sequence = vevent.get("sequence")
    assert sequence is not None
    assert int(sequence) == int(event.modified.timestamp())


@pytest.mark.django_db
def test_build_ics_dtstamp_from_modified():
    """Test that DTSTAMP is set from the modified timestamp."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Timestamped Event",
        description="",
        external_id="evt-dtstamp",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        timezone="UTC",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    dtstamp = vevent.get("dtstamp")
    assert dtstamp is not None


@pytest.mark.django_db
def test_build_ics_status_confirmed():
    """Test that STATUS is CONFIRMED for normal events."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Confirmed Event",
        description="",
        external_id="evt-status",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        timezone="UTC",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    vevent = events[0]

    status = vevent.get("status")
    assert status == "CONFIRMED"


@pytest.mark.django_db
def test_build_ics_prodid_and_version():
    """Test that PRODID and VERSION are correct in the calendar."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Test Event",
        description="",
        external_id="evt-prod",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        timezone="UTC",
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    cal = icalendar.Calendar.from_ical(ics_bytes)

    prodid = cal.get("prodid")
    version = cal.get("version")

    assert prodid is not None
    assert "Vinta Schedule" in str(prodid)
    assert version == "2.0"


@pytest.mark.django_db
def test_build_ics_missing_title_raises_value_error():
    """Test that events without title raise a ValueError."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="",  # Empty title
        description="",
        external_id="evt-no-title",
        start_time_tz_unaware=datetime.datetime(2025, 6, 21, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 21, 10, 0),
        timezone="UTC",
    )

    service = CalendarEventICSService()

    with pytest.raises(ValueError, match="Event must have a title"):
        service.build_ics(event)


@pytest.mark.parametrize(
    ("missing_field", "expected_message"),
    [
        ("title", "Event must have a title"),
        ("start_time", "Event must have a start_time"),
        ("end_time", "Event must have an end_time"),
        ("timezone", "Event must have a timezone"),
    ],
)
def test_build_ics_missing_required_field_raises_value_error(missing_field, expected_message):
    """Each missing required field must raise a ValueError.

    ``start_time``/``end_time`` are DB-computed GeneratedFields that are always
    populated on a persisted CalendarEvent, so their falsy branches cannot be
    exercised through a saved model instance. The builder is a pure, stateless
    function that only reads attributes, so a lightweight stand-in object is used
    to drive each required field to a falsy value independently.
    """
    attrs = {
        "title": "Some Event",
        "start_time": datetime.datetime(2025, 6, 21, 9, 0, tzinfo=datetime.UTC),
        "end_time": datetime.datetime(2025, 6, 21, 10, 0, tzinfo=datetime.UTC),
        "timezone": "UTC",
        "external_id": "evt-validation",
        "description": "",
        "modified": datetime.datetime(2025, 6, 20, 9, 0, tzinfo=datetime.UTC),
        "created": datetime.datetime(2025, 6, 19, 9, 0, tzinfo=datetime.UTC),
        "id": 1,
    }
    attrs[missing_field] = None
    event = SimpleNamespace(**attrs)

    service = CalendarEventICSService()

    with pytest.raises(ValueError, match=expected_message):
        service.build_ics(event)


@pytest.mark.django_db
def test_build_ics_multiple_events_each_valid():
    """Test building ICS for multiple different events."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    events = [
        baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title=f"Event {i}",
            description=f"Description {i}",
            external_id=f"evt-{i}",
            start_time_tz_unaware=datetime.datetime(2025, 6, 21 + i, 9, 0),
            end_time_tz_unaware=datetime.datetime(2025, 6, 21 + i, 10, 0),
            timezone="UTC",
        )
        for i in range(3)
    ]

    service = CalendarEventICSService()

    for event in events:
        ics_bytes = service.build_ics(event)

        # Each should parse successfully
        cal = icalendar.Calendar.from_ical(ics_bytes)
        events_in_cal = [c for c in cal.walk("VEVENT")]
        assert len(events_in_cal) == 1

        vevent = events_in_cal[0]
        assert vevent.get("uid") == event.external_id
        assert vevent.get("summary") == event.title
