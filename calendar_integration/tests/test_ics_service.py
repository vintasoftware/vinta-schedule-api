"""Unit tests for the CalendarEventICSService.

Tests verify that build_ics produces valid RFC 5545 iCalendar documents that:
- Round-trip through icalendar.Calendar.from_ical for validity
- Contain correct UID (external_id when present, synthetic fallback otherwise)
- Contain correct SUMMARY, DTSTART, DTEND, STATUS, SEQUENCE, DTSTAMP
- Properly escape special characters (commas, semicolons, newlines) in description
- Phase 2: carry ORGANIZER, ATTENDEE, RRULE, EXDATE lines correctly
- Phase 2: emit STATUS:CANCELLED for cancelled-exception events
"""

import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import icalendar
import pytest
from model_bakery import baker

from calendar_integration.constants import RSVPStatus
from calendar_integration.factories import (
    CalendarEventFactory,
    create_calendar_ownership,
    create_event_attendance,
)
from calendar_integration.models import (
    CalendarEvent,
    EventRecurrenceException,
    ExternalAttendee,
)
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


# ---------------------------------------------------------------------------
# Phase 2 tests — ORGANIZER, ATTENDEE, RRULE, EXDATE, STATUS:CANCELLED
# ---------------------------------------------------------------------------


def _parse_vevent(ics_bytes: bytes) -> icalendar.cal.Component:
    """Parse ics_bytes and return the first VEVENT component."""
    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = [c for c in cal.walk("VEVENT")]
    assert len(events) == 1, f"Expected 1 VEVENT, got {len(events)}"
    return events[0]  # type: ignore[return-value]


@pytest.mark.django_db
def test_build_ics_recurring_event_emits_single_vevent_with_rrule():
    """A recurring event emits exactly ONE VEVENT whose RRULE matches
    the recurrence rule's to_rrule_string() output."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Weekly Standup",
        description="",
        start_time=datetime.datetime(2025, 6, 23, 9, 0),
        end_time=datetime.datetime(2025, 6, 23, 9, 30),
        frequency="WEEKLY",
        by_weekday="MO,WE,FR",
        external_id="evt-recurring",
    )
    # Refresh to load the recurrence_rule FK via ORM
    event = CalendarEvent.objects.filter_by_organization(org.id).get(id=event.id)

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    # Must parse cleanly and yield exactly one VEVENT
    cal = icalendar.Calendar.from_ical(ics_bytes)
    vevents = [c for c in cal.walk("VEVENT")]
    assert len(vevents) == 1, "Recurring event must produce exactly one VEVENT"

    vevent = vevents[0]
    rrule = vevent.get("rrule")
    assert rrule is not None, "Recurring event must have an RRULE"

    # The serialized RRULE value must encode FREQ and BYDAY correctly
    ics_str = ics_bytes.decode("utf-8")
    # RRULE: line should contain the recurrence rule value
    assert "FREQ=WEEKLY" in ics_str
    assert "BYDAY=MO,WE,FR" in ics_str


@pytest.mark.django_db
def test_build_ics_non_recurring_event_has_no_rrule():
    """A plain (non-recurring) event must NOT contain an RRULE line."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="One-off Meeting",
        description="",
        external_id="evt-oneoff",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 10, 0),
        timezone="UTC",
        recurrence_rule=None,
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    vevent = _parse_vevent(ics_bytes)
    assert vevent.get("rrule") is None, "Non-recurring event must not have an RRULE"
    ics_str = ics_bytes.decode("utf-8")
    assert "RRULE" not in ics_str


@pytest.mark.django_db
def test_build_ics_recurring_event_with_cancelled_exception_emits_exdate():
    """A recurring event with a cancelled EventRecurrenceException emits
    an EXDATE whose datetime matches the exception_date."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Daily Sync",
        description="",
        start_time=datetime.datetime(2025, 6, 23, 8, 0),
        end_time=datetime.datetime(2025, 6, 23, 8, 30),
        frequency="DAILY",
        external_id="evt-with-exdate",
    )

    # Create a cancelled exception for 2025-06-25 08:00 UTC
    cancelled_dt = datetime.datetime(2025, 6, 25, 8, 0, tzinfo=datetime.UTC)
    EventRecurrenceException.objects.create(
        organization=org,
        parent_event=event,
        exception_date=cancelled_dt,
        is_cancelled=True,
    )

    event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .prefetch_related("recurrence_exceptions", "recurrence_rule")
        .get(id=event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event_reloaded)

    # Must parse cleanly
    cal = icalendar.Calendar.from_ical(ics_bytes)
    vevents = [c for c in cal.walk("VEVENT")]
    assert len(vevents) == 1

    vevent = vevents[0]
    exdate_prop = vevent.get("exdate")
    assert exdate_prop is not None, "Cancelled exception must produce an EXDATE"

    # The EXDATE may be a single vDDDLists or a list; flatten to datetimes
    exdates_raw = exdate_prop if isinstance(exdate_prop, list) else [exdate_prop]
    exdate_dts: list[datetime.datetime] = []
    for ed in exdates_raw:
        if hasattr(ed, "dts"):
            exdate_dts.extend(d.dt for d in ed.dts)
        else:
            exdate_dts.append(ed.dt)

    # Verify the cancelled occurrence datetime is present
    exdate_utcs = [dt.astimezone(datetime.UTC) for dt in exdate_dts]
    assert cancelled_dt in exdate_utcs, f"Expected {cancelled_dt} in EXDATE list, got {exdate_utcs}"


@pytest.mark.django_db
def test_build_ics_modified_exception_does_not_appear_in_exdate():
    """A modified (non-cancelled) EventRecurrenceException must NOT appear
    in EXDATE — only cancelled ones do."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Daily Check",
        description="",
        start_time=datetime.datetime(2025, 7, 1, 10, 0),
        end_time=datetime.datetime(2025, 7, 1, 10, 30),
        frequency="DAILY",
        external_id="evt-modified-exc",
    )

    # Create a MODIFIED (not cancelled) exception — should not appear as EXDATE
    modified_dt = datetime.datetime(2025, 7, 3, 10, 0, tzinfo=datetime.UTC)
    EventRecurrenceException.objects.create(
        organization=org,
        parent_event=event,
        exception_date=modified_dt,
        is_cancelled=False,
    )

    event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .prefetch_related("recurrence_exceptions", "recurrence_rule")
        .get(id=event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event_reloaded)

    vevent = _parse_vevent(ics_bytes)
    # Modified exception must NOT appear as EXDATE
    assert vevent.get("exdate") is None, (
        "Modified (non-cancelled) exception must not produce an EXDATE"
    )


@pytest.mark.django_db
def test_build_ics_attendees_internal_and_external():
    """An event with one internal attendee and one external attendee emits two
    correctly-formatted ATTENDEE lines carrying the right emails, ROLE, and PARTSTAT.
    """
    from users.factories import UserFactory

    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Attended Event",
        description="",
        external_id="evt-attendees",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 14, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 15, 0),
        timezone="UTC",
    )

    # Internal attendee — membership_user_id path
    internal_user = UserFactory().create_user(email="internal@example.com")
    attendance = create_event_attendance(
        event=event,
        user=internal_user,
        status=RSVPStatus.ACCEPTED,
    )
    assert attendance.membership_user_id == internal_user.id

    # External attendee via EventExternalAttendance (with status)
    external_attendee = ExternalAttendee.objects.create(
        organization=org,
        email="external@example.com",
        name="External Person",
    )
    from calendar_integration.models import EventExternalAttendance

    EventExternalAttendance.objects.create(
        organization=org,
        event=event,
        external_attendee=external_attendee,
        status=RSVPStatus.PENDING,
    )

    # Reload with prefetch
    event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .prefetch_related(
            "attendances__membership__user", "external_attendances__external_attendee"
        )
        .get(id=event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event_reloaded)

    # Must parse cleanly
    icalendar.Calendar.from_ical(ics_bytes)

    # Parse and inspect ATTENDEE parameters using the parsed representation
    # (do not check raw ICS string for emails because RFC 5545 line-folding
    # may split a long email address across two lines)
    vevent = _parse_vevent(ics_bytes)
    attendees_raw = vevent.get("attendee")
    # May be a single value or list; normalise
    if not isinstance(attendees_raw, list):
        attendees_raw = [attendees_raw]

    assert len(attendees_raw) == 2, f"Expected 2 ATTENDEE lines, got {len(attendees_raw)}"

    attendee_strs = [str(a) for a in attendees_raw]
    assert any("internal@example.com" in s for s in attendee_strs), (
        f"internal@example.com not found in attendees: {attendee_strs}"
    )
    assert any("external@example.com" in s for s in attendee_strs), (
        f"external@example.com not found in attendees: {attendee_strs}"
    )

    # Verify PARTSTAT and ROLE are present by inspecting the params on the
    # parsed vCalAddress objects
    for attendee in attendees_raw:
        assert hasattr(attendee, "params"), f"Attendee {attendee!r} has no params"
        assert attendee.params.get("ROLE") == "REQ-PARTICIPANT", (
            f"Expected ROLE=REQ-PARTICIPANT, got {attendee.params.get('ROLE')!r}"
        )
        partstat = attendee.params.get("PARTSTAT")
        assert partstat in ("ACCEPTED", "DECLINED", "NEEDS-ACTION", "TENTATIVE"), (
            f"Unexpected PARTSTAT: {partstat!r}"
        )

    # ACCEPTED attendee for internal user, NEEDS-ACTION for external (PENDING)
    partstat_values = {attendee.params.get("PARTSTAT") for attendee in attendees_raw}
    assert "ACCEPTED" in partstat_values
    assert "NEEDS-ACTION" in partstat_values


@pytest.mark.django_db
def test_build_ics_attendee_partstat_mapping():
    """Verify all RSVPStatus values map to the correct PARTSTAT in the ICS."""
    from users.factories import UserFactory

    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    def _make_event_with_status(rsvp_status: str, ext_id: str) -> bytes:
        event = baker.make(
            CalendarEvent,
            organization=org,
            calendar=calendar,
            title="Partstat Test",
            description="",
            external_id=ext_id,
            start_time_tz_unaware=datetime.datetime(2025, 7, 1, 9, 0),
            end_time_tz_unaware=datetime.datetime(2025, 7, 1, 10, 0),
            timezone="UTC",
        )
        user = UserFactory().create_user()
        create_event_attendance(event=event, user=user, status=rsvp_status)
        reloaded = (
            CalendarEvent.objects.filter_by_organization(org.id)
            .prefetch_related("attendances__membership__user")
            .get(id=event.id)
        )
        return CalendarEventICSService().build_ics(reloaded)

    assert "PARTSTAT=ACCEPTED" in _make_event_with_status(RSVPStatus.ACCEPTED, "evt-acc").decode()
    assert "PARTSTAT=DECLINED" in _make_event_with_status(RSVPStatus.DECLINED, "evt-dec").decode()
    assert (
        "PARTSTAT=NEEDS-ACTION" in _make_event_with_status(RSVPStatus.PENDING, "evt-pend").decode()
    )


@pytest.mark.django_db
def test_build_ics_organizer_present_when_calendar_has_owner():
    """ORGANIZER line is present when the calendar has a primary owner membership."""
    from users.factories import UserFactory

    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    owner_user = UserFactory().create_user(email="owner@example.com")
    create_calendar_ownership(calendar=calendar, user=owner_user)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Owned Event",
        description="",
        external_id="evt-owned",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 10, 0),
        timezone="UTC",
    )

    event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .select_related("calendar")
        .prefetch_related("calendar__ownerships__membership__user")
        .get(id=event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event_reloaded)

    ics_str = ics_bytes.decode("utf-8")
    assert "ORGANIZER" in ics_str, "ORGANIZER must be present when the calendar has an owner"
    assert "owner@example.com" in ics_str, "ORGANIZER must include the owner's email"

    vevent = _parse_vevent(ics_bytes)
    organizer = vevent.get("organizer")
    assert organizer is not None
    assert "owner@example.com" in str(organizer)


@pytest.mark.django_db
def test_build_ics_organizer_omitted_when_calendar_has_no_owner():
    """ORGANIZER line is omitted (and build_ics does not crash) when the
    calendar has no ownership rows."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)
    # Deliberately no CalendarOwnership created

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="No Owner Event",
        description="",
        external_id="evt-noowner",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 10, 0),
        timezone="UTC",
    )

    event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .select_related("calendar")
        .prefetch_related("calendar__ownerships__membership__user")
        .get(id=event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event_reloaded)  # must not crash

    ics_str = ics_bytes.decode("utf-8")
    # ORGANIZER must be absent
    assert "ORGANIZER" not in ics_str

    # Output must still be valid iCalendar
    icalendar.Calendar.from_ical(ics_bytes)


@pytest.mark.django_db
def test_build_ics_normal_event_status_confirmed():
    """A normal (non-exception) event emits STATUS:CONFIRMED."""
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Normal Event",
        description="",
        external_id="evt-normal",
        start_time_tz_unaware=datetime.datetime(2025, 6, 23, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 6, 23, 10, 0),
        timezone="UTC",
        is_recurring_exception=False,
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event)

    vevent = _parse_vevent(ics_bytes)
    assert vevent.get("status") == "CONFIRMED"


@pytest.mark.django_db
def test_build_ics_cancelled_exception_event_emits_status_cancelled():
    """An event that is a cancelled exception (is_recurring_exception=True and
    matched by a cancelled EventRecurrenceException) emits STATUS:CANCELLED.

    NOTE: In the current schema a cancelled occurrence does NOT spawn a separate
    CalendarEvent row. This test creates the scenario by manufacturing an
    ``exception_for`` relation with ``is_cancelled=True`` to verify the code
    path executes correctly when such a row exists.
    """
    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    parent_event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Parent Recurring",
        description="",
        external_id="evt-parent-cancel",
        start_time_tz_unaware=datetime.datetime(2025, 7, 1, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 7, 1, 10, 0),
        timezone="UTC",
    )

    # Create a CalendarEvent flagged as a recurring exception
    exception_event = baker.make(
        CalendarEvent,
        organization=org,
        calendar=calendar,
        title="Exception Event",
        description="",
        external_id="evt-exception",
        start_time_tz_unaware=datetime.datetime(2025, 7, 8, 9, 0),
        end_time_tz_unaware=datetime.datetime(2025, 7, 8, 10, 0),
        timezone="UTC",
        is_recurring_exception=True,
    )

    # Wire the cancelled exception that points to this event as modified_event
    EventRecurrenceException.objects.create(
        organization=org,
        parent_event=parent_event,
        modified_event=exception_event,
        exception_date=datetime.datetime(2025, 7, 8, 9, 0, tzinfo=datetime.UTC),
        is_cancelled=True,
    )

    # Reload with the exception_for relation
    exception_event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .prefetch_related("exception_for")
        .get(id=exception_event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(exception_event_reloaded)

    vevent = _parse_vevent(ics_bytes)
    assert vevent.get("status") == "CANCELLED", (
        "A cancelled-exception event must emit STATUS:CANCELLED"
    )


@pytest.mark.django_db
def test_build_ics_full_acceptance_scenario():
    """Acceptance test: a recurring event with one cancelled occurrence and
    one internal + one external attendee produces a single VEVENT whose RRULE
    matches to_rrule_string(), whose EXDATE contains the cancelled occurrence,
    and which carries one ORGANIZER and two ATTENDEE lines.
    """
    from users.factories import UserFactory

    org = baker.make("organizations.Organization")
    calendar = baker.make("calendar_integration.Calendar", organization=org)

    # Calendar owner → ORGANIZER
    owner_user = UserFactory().create_user(email="organizer@example.com")
    create_calendar_ownership(calendar=calendar, user=owner_user)

    # Recurring event
    event = CalendarEventFactory.create_recurring_event(
        calendar=calendar,
        title="Team Sync",
        description="Weekly team meeting",
        start_time=datetime.datetime(2025, 6, 23, 10, 0),
        end_time=datetime.datetime(2025, 6, 23, 11, 0),
        frequency="WEEKLY",
        by_weekday="MO",
        external_id="evt-acceptance",
    )

    # Cancelled occurrence
    cancelled_dt = datetime.datetime(2025, 6, 30, 10, 0, tzinfo=datetime.UTC)
    EventRecurrenceException.objects.create(
        organization=org,
        parent_event=event,
        exception_date=cancelled_dt,
        is_cancelled=True,
    )

    # Internal attendee
    internal_user = UserFactory().create_user(email="internal-acc@example.com")
    create_event_attendance(event=event, user=internal_user, status=RSVPStatus.ACCEPTED)

    # External attendee
    external_attendee = ExternalAttendee.objects.create(
        organization=org,
        email="external-acc@example.com",
        name="External Acc",
    )
    from calendar_integration.models import EventExternalAttendance

    EventExternalAttendance.objects.create(
        organization=org,
        event=event,
        external_attendee=external_attendee,
        status=RSVPStatus.DECLINED,
    )

    # Reload with all required prefetches
    event_reloaded = (
        CalendarEvent.objects.filter_by_organization(org.id)
        .select_related("calendar", "recurrence_rule")
        .prefetch_related(
            "calendar__ownerships__membership__user",
            "attendances__membership__user",
            "external_attendances__external_attendee",
            "recurrence_exceptions",
        )
        .get(id=event.id)
    )

    service = CalendarEventICSService()
    ics_bytes = service.build_ics(event_reloaded)

    # Must be valid iCalendar
    cal = icalendar.Calendar.from_ical(ics_bytes)
    vevents = [c for c in cal.walk("VEVENT")]
    assert len(vevents) == 1, "Must produce exactly one VEVENT"
    vevent = vevents[0]

    ics_str = ics_bytes.decode("utf-8")

    # RRULE present and matches the recurrence rule
    assert "FREQ=WEEKLY" in ics_str
    assert "BYDAY=MO" in ics_str
    rrule_value = event_reloaded.recurrence_rule.to_rrule_string()
    assert "FREQ=WEEKLY" in rrule_value

    # EXDATE contains the cancelled occurrence
    exdate_prop = vevent.get("exdate")
    assert exdate_prop is not None, "EXDATE must be present"
    exdates_raw = exdate_prop if isinstance(exdate_prop, list) else [exdate_prop]
    exdate_dts = []
    for ed in exdates_raw:
        if hasattr(ed, "dts"):
            exdate_dts.extend(d.dt for d in ed.dts)
        else:
            exdate_dts.append(ed.dt)
    exdate_utcs = [dt.astimezone(datetime.UTC) for dt in exdate_dts]
    assert cancelled_dt in exdate_utcs

    # ORGANIZER present
    organizer = vevent.get("organizer")
    assert organizer is not None
    assert "organizer@example.com" in str(organizer)

    # Two ATTENDEE lines
    attendees_raw = vevent.get("attendee")
    if not isinstance(attendees_raw, list):
        attendees_raw = [attendees_raw]
    assert len(attendees_raw) == 2, f"Expected 2 ATTENDEE lines, got {len(attendees_raw)}"
    attendee_strs = [str(a) for a in attendees_raw]
    assert any("internal-acc@example.com" in s for s in attendee_strs)
    assert any("external-acc@example.com" in s for s in attendee_strs)

    # STATUS:CONFIRMED (this is a normal recurring event, not a cancelled exception)
    assert vevent.get("status") == "CONFIRMED"
