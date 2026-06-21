"""ICS (iCalendar) builder service for calendar events.

Generates a valid RFC 5545 iCalendar (.ics) document for a single CalendarEvent.
This is a pure, read-only service that takes a fully-loaded CalendarEvent
and returns bytes containing the ICS representation.

Callers must pass a fully-loaded CalendarEvent with related data prefetched.
In later phases when participants and recurrence are added, the required
prefetch set will be documented here.
"""

import icalendar

from calendar_integration.models import CalendarEvent


class CalendarEventICSService:
    """Builder service for converting a CalendarEvent to RFC 5545 iCalendar format.

    Pure, stateless service that takes a CalendarEvent and returns its ICS bytes.
    Performs no database writes, no authentication, no organization resolution.
    All required context must be present in the passed event object.
    """

    def build_ics(self, event: CalendarEvent) -> bytes:
        """Generate an RFC 5545 iCalendar document for a calendar event.

        Args:
            event: A fully-loaded CalendarEvent instance.

        Returns:
            bytes: The ICS document as RFC 5545-compliant bytes.

        Raises:
            ValueError: If required fields are missing.
        """
        # Validate required fields
        if not event.title:
            raise ValueError("Event must have a title")
        if not event.start_time:
            raise ValueError("Event must have a start_time")
        if not event.end_time:
            raise ValueError("Event must have an end_time")
        if not event.timezone:
            raise ValueError("Event must have a timezone")

        # Create the calendar container
        cal = icalendar.Calendar()
        cal.add("prodid", "-//Vinta Schedule//Vinta Schedule API//EN")
        cal.add("version", "2.0")

        # Create the event
        vevent = icalendar.Event()

        # UID: use external_id if present, otherwise derive from event id
        if event.external_id:
            vevent.add("uid", event.external_id)
        else:
            vevent.add("uid", f"event-{event.id}@vinta-schedule")

        # Summary (title)
        vevent.add("summary", event.title)

        # Start and end times (timezone-aware)
        vevent.add("dtstart", event.start_time)
        vevent.add("dtend", event.end_time)

        # DTSTAMP: use a deterministic value from the event's modified timestamp
        vevent.add("dtstamp", event.modified or event.created)

        # Description
        if event.description:
            vevent.add("description", event.description)

        # STATUS
        vevent.add("status", "CONFIRMED")

        # SEQUENCE: derive from modified timestamp or use 0
        if event.modified:
            # Use int(modified.timestamp()) as a stable sequence number
            sequence = int(event.modified.timestamp())
            vevent.add("sequence", sequence)
        else:
            vevent.add("sequence", 0)

        # Add the event to the calendar
        cal.add_component(vevent)

        # Return as bytes
        return cal.to_ical()
