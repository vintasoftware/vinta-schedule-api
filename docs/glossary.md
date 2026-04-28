# Glossary

Quick definitions for every domain term used in [docs/](.) and in
[calendar_integration/](../calendar_integration/). When introducing a new
domain term in code or docs, add it here in the same change.

Links point at the file or model that owns the canonical definition.

---

### Available Time

A positive declaration that a calendar is **open** for a span of time.
Modelled by `AvailableTime`. Only meaningful for calendars with
`manage_available_windows=True`. Usually recurring (e.g. weekly clinic
hours).
→ [concepts/availability.md](concepts/availability.md)

### Blocked Time

A negative declaration that a calendar is **busy** for a span of time,
even when there's no event on it. Modelled by `BlockedTime`. Used for
vacations, externally-synced busy time, and bundle/group-side
propagation.
→ [concepts/availability.md](concepts/availability.md)

### Bookable Slot Proposal

A concrete `(start_time, end_time)` window where every slot of a
`CalendarGroup` can be satisfied. Returned by
`CalendarGroupService.find_bookable_slots`.
→ [concepts/calendar-groups.md](concepts/calendar-groups.md#availability-and-bookable-slots)

### Bulk Modification

A change applied to a recurring series from a chosen date forward —
implemented as a **split**: the original master is truncated with
`UNTIL`, and a new continuation master with the modified rule takes
over. Tracked by `EventBulkModification`,
`BlockedTimeBulkModification`, `AvailableTimeBulkModification`.
→ [concepts/recurrence.md](concepts/recurrence.md#bulk-modifications-and-splits)

### Bundle Calendar

A `Calendar` with `calendar_type=BUNDLE` that aggregates a fixed list
of child calendars and acts as a single bookable façade. Bookings on
the bundle propagate to every child as either a representation event
(internal child) or a `BlockedTime` (external child).
→ [concepts/calendar-bundles.md](concepts/calendar-bundles.md)

### Calendar

The atomic bookable unit. Holds events, blocked times, and available
times. Has a `calendar_type` (`PERSONAL` / `RESOURCE` / `VIRTUAL` /
`BUNDLE`) and a `provider` (`INTERNAL` / `GOOGLE` / `MICROSOFT` /
`APPLE` / `ICS`). Belongs to one `Organization`.
→ [concepts/calendars.md](concepts/calendars.md)

### Calendar Event

A booking on a calendar. Has `start_time`, `end_time`, `timezone`,
`attendances`, `external_attendees`, `resource_allocations`. May be
recurring. May be tied to a `CalendarGroup` and/or a bundle.
→ [concepts/events.md](concepts/events.md)

### Calendar Event Group Selection

A row recording that, for a given grouped booking, a particular
`Calendar` was picked to satisfy a particular `CalendarGroupSlot`.
Modelled by `CalendarEventGroupSelection`. One booking has one row per
(slot, calendar) pick.
→ [concepts/calendar-groups.md](concepts/calendar-groups.md#models)

### Calendar Group

A booking template. Defines named slots, each with a pool of candidate
calendars and a `required_count`. Bookings are made by picking the
required calendars per slot; the system enforces simultaneous
availability across all picks. Modelled by `CalendarGroup`.
→ [concepts/calendar-groups.md](concepts/calendar-groups.md)

### Calendar Group Slot

A required role inside a `CalendarGroup` — e.g. "Physicians", "Rooms",
"Nurses". Holds a pool of candidate calendars and the `required_count`
that must be picked at booking time. Modelled by `CalendarGroupSlot`.
→ [concepts/calendar-groups.md](concepts/calendar-groups.md#models)

### Calendar Group Slot Membership

The through-table row linking a `Calendar` into a
`CalendarGroupSlot`'s pool. Modelled by `CalendarGroupSlotMembership`.

### Calendar Ownership

The link between a `User` and a `Calendar`. Owners can manage the
calendar (create events, declare availability, etc.). One per (user,
calendar); `is_default=True` marks the user's default-target calendar.
Modelled by `CalendarOwnership`.

### Capacity (resource calendar)

`Calendar.capacity` — the maximum simultaneous attendees a resource
calendar can host (e.g. an infusion bay with six chairs). Only
applicable to `RESOURCE` calendars.

### Child Calendar

A `Calendar` that belongs to a bundle calendar via
`ChildrenCalendarRelationship`. Exactly one child is marked
`is_primary=True` per bundle.
→ [concepts/calendar-bundles.md](concepts/calendar-bundles.md)

### Continuation (recurrence)

The new recurring object created when a series is split by a bulk
modification. Carries `bulk_modification_parent` pointing at the
original master and a derived `RecurrenceRule` starting at the
modification date.
→ [concepts/recurrence.md](concepts/recurrence.md#bulk-modifications-and-splits)

### Exception (recurrence)

A single-occurrence override of a recurring object. Modelled by
`EventRecurrenceException`, `BlockedTimeRecurrenceException`, and
`AvailableTimeRecurrenceException`. `is_cancelled=True` skips the
occurrence; `is_cancelled=False` substitutes a modified row.
→ [concepts/recurrence.md](concepts/recurrence.md)

### External Attendee

A non-user participant on an event, identified by email + name.
Typically the patient on a clinic appointment, or a referring
physician outside the organization. Modelled by `ExternalAttendee` /
`EventExternalAttendance`.
→ [concepts/events.md](concepts/events.md#attendees-vs-external-attendees)

### Managed Availability Windows (`manage_available_windows`)

A flag on `Calendar`. When `True`, the calendar is bookable **only**
during explicit `AvailableTime` rows; outside those, it's treated as
fully booked. When `False`, anything that isn't a `CalendarEvent` or
`BlockedTime` is implicitly free.
→ [concepts/availability.md](concepts/availability.md#managed-vs-unmanaged-calendars--when-to-flip-the-bit)

### Organization

The multi-tenant boundary. Every `Calendar`, event, and ancillary row
belongs to exactly one `Organization` and is filtered by it through
`OrganizationModel` / `OrganizationForeignKey` (defined in
[organizations/models.py](../organizations/models.py)).

### Organization Membership

A `User`'s membership in an `Organization`. Carries the user's `role`
(`MEMBER` / `ADMIN`); admins get elevated permissions on the org's
resources (e.g. `can_manage_calendar_group`).

### Personal Calendar

A `Calendar` with `calendar_type=PERSONAL`. Belongs to a person; the
person attends events on it.
→ [concepts/calendars.md](concepts/calendars.md#personal)

### Primary (bundle)

The child calendar of a bundle marked `is_primary=True`. Hosts the
canonical event for any booking made on the bundle; non-primary
children get representation events or `BlockedTime` rows.
→ [concepts/calendar-bundles.md](concepts/calendar-bundles.md)

### Primary (group)

The calendar a grouped booking lands on as its `CalendarEvent.calendar_fk`
— picked as the first selection of the lowest-`order` slot of the
group.
→ [concepts/calendar-groups.md](concepts/calendar-groups.md#booking-semantics--calendargroupservicecreate_grouped_event)

### Pool (slot pool)

The set of `Calendar`s available to satisfy a given
`CalendarGroupSlot`. Materialized as the slot's
`CalendarGroupSlotMembership` rows.

### Provider

The system that owns a calendar's canonical data:
`INTERNAL`, `GOOGLE`, `MICROSOFT`, `APPLE`, `ICS`. Drives sync
behavior. See `CalendarProvider` in
[calendar_integration/constants.py](../calendar_integration/constants.py).

### Recurrence Rule

An [RFC 5545](https://datatracker.ietf.org/doc/html/rfc5545#section-3.8.5.3)
RRULE attached to a `CalendarEvent`, `BlockedTime`, or `AvailableTime`.
Defines the cadence (`FREQ`, `INTERVAL`, `BYDAY`, …). Modelled by
`RecurrenceRule`.
→ [concepts/recurrence.md](concepts/recurrence.md)

### Recurring Mixin (`RecurringMixin`)

The shared abstract base providing recurrence fields and
`get_occurrences_in_range` to `CalendarEvent`, `BlockedTime`, and
`AvailableTime`.

### Required Count

`CalendarGroupSlot.required_count` — the number of calendars from a
slot's pool that must be picked at booking time. Default `1`. Larger
values express "two nurses required", "two attendings + one fellow", etc.
→ [concepts/calendar-groups.md](concepts/calendar-groups.md#why-slots-have-required_count)

### Resource Allocation

The link between a `CalendarEvent` and a `RESOURCE` `Calendar` with
its own RSVP status. Modelled by `ResourceAllocation`. Distinct from
`CalendarEventGroupSelection`, which records group-slot picks for
group bookings.
→ [concepts/events.md](concepts/events.md#resource-allocations)

### Resource Calendar

A `Calendar` with `calendar_type=RESOURCE`. Represents a thing (room,
device, suite) rather than a person. Optionally has `capacity`.
→ [concepts/calendars.md](concepts/calendars.md#resource)

### RSVP Status

`accepted` / `declined` / `pending`. Used on `EventAttendance`,
`EventExternalAttendance`, and `ResourceAllocation`.

### Slot (group slot)

Short for `CalendarGroupSlot` — see above.

### Split (recurrence)

The act of creating a continuation from a master recurring object
during a bulk modification. The master is truncated with `UNTIL`, the
continuation takes over with a modified rule.
→ [concepts/recurrence.md](concepts/recurrence.md#bulk-modifications-and-splits)

### Virtual Calendar

A `Calendar` with `calendar_type=VIRTUAL`. Represents an online
endpoint (telehealth session, video meeting room) rather than a
physical one.
→ [concepts/calendars.md](concepts/calendars.md#virtual)
