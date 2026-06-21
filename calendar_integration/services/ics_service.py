"""ICS (iCalendar) builder service for calendar events.

Generates a valid RFC 5545 iCalendar (.ics) document for a single CalendarEvent.
This is a pure, read-only service that takes a fully-loaded CalendarEvent
and returns bytes containing the ICS representation.

Required prefetch set (callers must apply to avoid N+1 queries)
---------------------------------------------------------------
To prevent N+1 queries, callers must apply the following select_related and
prefetch_related before passing the event to build_ics::

    event = (
        CalendarEvent.objects
        .filter_by_organization(org_id)
        .filter(id=event_id)
        .select_related(
            # Calendar owner chain for ORGANIZER
            "calendar",
        )
        .prefetch_related(
            # Calendar ownership → membership → user for ORGANIZER
            "calendar__ownerships__membership__user",
            # Internal attendees → membership → user for ATTENDEE lines
            "attendances__membership__user",
            # External attendees → ExternalAttendee for ATTENDEE lines
            "external_attendances__external_attendee",
            # Recurrence rule for RRULE
            "recurrence_rule",
            # Cancelled recurrence exceptions for EXDATE
            "recurrence_exceptions",
        )
        .get()
    )

RSVPStatus → PARTSTAT mapping
------------------------------
- RSVPStatus.ACCEPTED  → PARTSTAT=ACCEPTED
- RSVPStatus.DECLINED  → PARTSTAT=DECLINED
- RSVPStatus.PENDING   → PARTSTAT=NEEDS-ACTION
- (unknown)            → PARTSTAT=NEEDS-ACTION
"""

import icalendar

from calendar_integration.constants import RSVPStatus
from calendar_integration.models import CalendarEvent, EventAttendance


# Map the project's RSVPStatus to RFC 5545 PARTSTAT values.
_RSVP_TO_PARTSTAT: dict[str, str] = {
    RSVPStatus.ACCEPTED: "ACCEPTED",
    RSVPStatus.DECLINED: "DECLINED",
    RSVPStatus.PENDING: "NEEDS-ACTION",
}


class CalendarEventICSService:
    """Builder service for converting a CalendarEvent to RFC 5545 iCalendar format.

    Pure, stateless service that takes a CalendarEvent and returns its ICS bytes.
    Performs no database writes, no authentication, no organization resolution.
    All required context must be present in the passed event object.

    See the module docstring for the full required prefetch set.
    """

    def build_ics(self, event: CalendarEvent) -> bytes:
        """Generate an RFC 5545 iCalendar document for a calendar event.

        Args:
            event: A fully-loaded CalendarEvent instance with the prefetch set
                documented in the module docstring applied by the caller.

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

        # STATUS: CANCELLED when the event is itself a cancelled exception;
        # CONFIRMED otherwise.
        if event.is_recurring_exception and self._is_cancelled_exception(event):
            vevent.add("status", "CANCELLED")
        else:
            vevent.add("status", "CONFIRMED")

        # SEQUENCE: derive from modified timestamp or use 0
        if event.modified:
            # Use int(modified.timestamp()) as a stable sequence number
            sequence = int(event.modified.timestamp())
            vevent.add("sequence", sequence)
        else:
            vevent.add("sequence", 0)

        # ORGANIZER: calendar primary owner membership email (omit if unresolvable)
        organizer_email = self._resolve_organizer_email(event)
        if organizer_email:
            vevent.add("organizer", f"mailto:{organizer_email}")

        # ATTENDEE lines (internal members + external attendees)
        for attendee_cal_address in self._build_attendees(event):
            vevent.add("attendee", attendee_cal_address)

        # RRULE: emit when the event has a recurrence rule
        if event.recurrence_rule is not None:
            rrule_value = event.recurrence_rule.to_rrule_string()
            # to_rrule_string() returns e.g. "FREQ=WEEKLY;BYDAY=MO,WE"
            # (no "RRULE:" prefix). Parse into a dict for icalendar so it
            # produces a single valid RRULE: line.
            vevent.add("rrule", icalendar.vRecur.from_ical(rrule_value))

        # EXDATE: cancelled recurrence exceptions
        for exdate_dt in self._collect_cancelled_exdates(event):
            vevent.add("exdate", exdate_dt)

        # Add the event to the calendar
        cal.add_component(vevent)

        # Return as bytes
        return cal.to_ical()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_cancelled_exception(self, event: CalendarEvent) -> bool:
        """Return True when *event* is the modified-exception event that represents
        a cancellation (i.e. it was spawned from a parent recurring event to mark
        the occurrence as cancelled).

        A CalendarEvent is considered a cancelled exception when
        ``is_recurring_exception=True`` and it appears as the ``modified_event``
        on a cancelled ``EventRecurrenceException`` row for its parent.

        NOTE: In the current schema a cancelled occurrence does NOT spawn a
        separate CalendarEvent row; ``EventRecurrenceException.is_cancelled=True``
        and ``modified_event`` is NULL. The ``is_recurring_exception`` flag on a
        CalendarEvent therefore means the event is a *modified* (not cancelled)
        occurrence.  A truly-cancelled occurrence has no corresponding
        CalendarEvent at all — only an ``EventRecurrenceException`` row with
        ``is_cancelled=True``.

        For ICS export purposes the STATUS:CANCELLED path is therefore not
        reachable for ordinary events.  We keep the guard here so that if in
        future a cancelled CalendarEvent row is created (``is_recurring_exception``
        + matched by an ``exception_for`` relation with ``is_cancelled=True``), the
        builder handles it correctly without code change.
        """
        try:
            exception_for = event.exception_for.first()  # type: ignore[union-attr]
        except AttributeError:
            return False
        return exception_for is not None and exception_for.is_cancelled

    def _resolve_organizer_email(self, event: CalendarEvent) -> str | None:
        """Resolve the organizer email from the event's calendar primary owner.

        Traverses: event → calendar → ownerships → membership → user → email.
        Returns None if any step is unresolvable (avoids crashing and correctly
        omits ORGANIZER in the ICS output).
        """
        try:
            calendar = event.calendar
            if calendar is None:
                return None
            # CalendarOwnership.membership is an OrganizationMembershipForeignKey
            # which joins on (organization_id, membership_user_id). Use first()
            # since a calendar may have multiple owners; take the first for ORGANIZER.
            ownership = calendar.ownerships.first()
            if ownership is None:
                return None
            membership = ownership.membership
            if membership is None:
                return None
            user = membership.user
            if user is None:
                return None
            return user.email or None
        except AttributeError:
            return None

    def _build_attendees(self, event: CalendarEvent) -> list[icalendar.vCalAddress]:
        """Build the list of ATTENDEE cal-address values for the VEVENT.

        Combines:
        - Internal attendees (via EventAttendance → membership → user.email)
        - External attendees (via EventExternalAttendance → external_attendee.email)

        PARTSTAT and ROLE parameters are added per RFC 5545 §3.2.6 / §3.2.12.
        ROLE defaults to REQ-PARTICIPANT. PARTSTAT maps RSVPStatus → RFC value.
        """
        attendees: list[icalendar.vCalAddress] = []

        # Internal attendees
        for attendance in event.attendances.all():
            email = self._resolve_internal_attendee_email(attendance)
            if not email:
                continue
            partstat = _RSVP_TO_PARTSTAT.get(attendance.status, "NEEDS-ACTION")
            cal_address = icalendar.vCalAddress(f"mailto:{email}")
            cal_address.params["ROLE"] = "REQ-PARTICIPANT"
            cal_address.params["PARTSTAT"] = partstat
            attendees.append(cal_address)

        # External attendees (via EventExternalAttendance for status)
        for ext_attendance in event.external_attendances.all():
            ext_attendee = ext_attendance.external_attendee
            if ext_attendee is None:
                continue
            email = ext_attendee.email
            if not email:
                continue
            partstat = _RSVP_TO_PARTSTAT.get(ext_attendance.status, "NEEDS-ACTION")
            cal_address = icalendar.vCalAddress(f"mailto:{email}")
            cal_address.params["ROLE"] = "REQ-PARTICIPANT"
            cal_address.params["PARTSTAT"] = partstat
            attendees.append(cal_address)

        return attendees

    def _resolve_internal_attendee_email(self, attendance: EventAttendance) -> str | None:
        """Resolve the user email for an internal EventAttendance row.

        Traverses: attendance → membership → user → email.
        Returns None if any step is unresolvable.
        """
        try:
            membership = attendance.membership
            if membership is None:
                return None
            user = membership.user
            if user is None:
                return None
            return user.email or None
        except AttributeError:
            return None

    def _collect_cancelled_exdates(self, event: CalendarEvent) -> list[object]:
        """Collect the cancelled-occurrence datetimes for EXDATE lines.

        Iterates over ``event.recurrence_exceptions`` (the ``EventRecurrenceException``
        related manager, related_name="recurrence_exceptions") and returns the
        ``exception_date`` of every row where ``is_cancelled=True``.

        The returned datetimes are tz-aware; ``EventRecurrenceException.exception_date``
        is a plain DateTimeField. If the stored value is naive we leave it as-is
        (icalendar handles naive datetimes as floating) — the correct approach is for
        callers to store tz-aware values, which the service layer already does.
        """
        exdates: list[object] = []
        try:
            exceptions = event.recurrence_exceptions.all()  # type: ignore[union-attr]
        except AttributeError:
            return exdates

        for exc in exceptions:
            if exc.is_cancelled:
                exdates.append(exc.exception_date)

        return exdates
