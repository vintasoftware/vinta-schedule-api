# Events

A **`CalendarEvent`** is a booking. It always lives on exactly one
`Calendar` (`calendar_fk`) and has a `start_time` / `end_time` plus an
IANA `timezone`. Around that core, it carries:

| Field | What it models |
|-------|----------------|
| `attendees` (via `EventAttendance`) | Internal users participating, with RSVP status. |
| `external_attendees` (via `EventExternalAttendance`) | Non-user attendees identified by email + name. |
| `resources` (via `ResourceAllocation`) | Resource calendars allocated to the event. |
| `bundle_calendar`, `bundle_primary_event`, `is_bundle_primary` | Bundle membership (see [calendar-bundles.md](calendar-bundles.md)). |
| `calendar_group`, `group_selections` | Group booking metadata (see [calendar-groups.md](calendar-groups.md)). |
| `recurrence_rule`, `recurrence_id`, `parent_recurring_object`, `is_recurring_exception` | Recurrence (see [recurrence.md](recurrence.md)). |
| `bulk_modification_parent` | If the event is a continuation produced by a bulk modification split. |
| `external_id` | Stable id from the upstream provider for synced events. |

## Attendees vs. external attendees

- **`EventAttendance`** is for users with an account in the system. The
  RSVP status (`accepted`, `declined`, `pending`) and downstream
  notification logic use it. Example: an internal cardiologist invited to
  a tumour-board video call.
- **`ExternalAttendee` / `EventExternalAttendance`** is for
  non-account participants identified by email — typically the patient
  on a clinic appointment or a referring physician copied on a consult.

Both attend the same event; they're separate models because the system
knows much more about internal users (permissions, notification
preferences, calendar ownerships) than it does about external ones.

### Healthcare examples

- **Outpatient consult**: 1 attendance (the physician), 1 external
  attendee (the patient). The room is a `ResourceAllocation`.
- **Multi-disciplinary tumour board**: 6 attendances (oncologist,
  radiologist, pathologist, surgeon, nurse navigator, clinical fellow).
  No external attendees. One virtual calendar resource (the Zoom link).
- **Pre-op consultation with family**: 1 attendance (surgeon), 3 external
  attendees (patient + two family members), 1 resource (consult room).
- **Standing weekly grand rounds**: 40+ attendances, recurring weekly,
  resource = lecture-hall calendar.

## Resource allocations

`ResourceAllocation` ties an event to one or more **resource calendars**
(see [calendars.md](calendars.md#resource)). The allocation has its own
RSVP status (think: "the OR-2 calendar provisionally accepts" pending
confirmation by the OR scheduler).

Examples:

- **Surgery in OR-3 with the C-arm**: two resource allocations — `OR-3`
  and `C-arm fluoroscopy unit`. If the C-arm is double-booked, the
  scheduler can decline its allocation and reroute.
- **Infusion-bay chair + IV pump**: two allocations on a 4-hour infusion
  appointment.

> **Note on bookings via `CalendarGroup`**: when a slot of a group is
> filled with a *resource* calendar, the per-slot picks are stored in
> `CalendarEventGroupSelection` rather than `ResourceAllocation`. The two
> models coexist: `ResourceAllocation` is the older "this event uses
> these resources" mechanism; `CalendarEventGroupSelection` is the
> "which calendars satisfied each slot of the booking template" record.

## Bundle and group fields

A `CalendarEvent` knows whether it was created through a higher-level
booking primitive:

- `bundle_calendar` is non-null when the event was created via a
  `BUNDLE` calendar. `is_bundle_primary=True` marks the canonical event
  (the one synced to the external provider); other child calendars get a
  representation event or a `BlockedTime`. See
  [calendar-bundles.md](calendar-bundles.md).
- `calendar_group` is non-null when the event was booked via a
  `CalendarGroup`. The companion `CalendarEventGroupSelection` rows
  record which calendar from each slot's pool was picked. See
  [calendar-groups.md](calendar-groups.md).

Both fields are independent of recurrence — a recurring weekly tumour
board can absolutely be a grouped event, with each occurrence inheriting
the same selections.

## Lifecycle — creating, updating, cancelling

`CalendarService` (in [calendar_integration/services/calendar_service.py](../../calendar_integration/services/calendar_service.py))
is the main entry point. It:

- Validates availability via `only_calendars_available_in_ranges`.
- Persists the event.
- Triggers side-effects (provider sync, attendee invites,
  notifications).

For grouped/bundled bookings, callers should use the higher-level
services (`CalendarGroupService.create_grouped_event`,
`CalendarService.create_bundle_calendar` + `_create_bundle_event`)
rather than `create_event` directly — those services handle picking the
primary calendar, propagating to children, and writing the per-slot or
per-bundle metadata in one transaction.

### Public API — `updateCalendarEvent`

The public GraphQL API's `updateCalendarEvent` mutation updates a single-calendar
event's **title, description, internal attendees, external attendees and client
identifiers**. Every field besides `eventId` is `UNSET`-defaulted: **omitting a field
leaves it exactly as stored**. A caller that supplies only `title` does not touch
attendees or identifiers.

That "omitted vs. supplied" distinction goes all the way down: `CalendarEventInputData`
is itself tri-state on `title`, `description`, `attendances` and `external_attendances`
(`None` = leave untouched), so the resolver passes `UNSET` straight through as `None`
and `CalendarEventService.update_event` skips the corresponding write entirely — no
assignment, no attendee reconciliation, no attendee webhook. It deliberately does **not**
read the event's current values and re-send them: that older shape could revert a
concurrent update to a field this caller never named, and re-sending every existing
external attendee (id included) put them all on `update_event`'s update-in-place branch,
emitting one `CALENDAR_EVENT_ATTENDEE_UPDATED` webhook per attendee on a title-only
update.

On `create_event` there is nothing to leave untouched, so `None` behaves as the empty
value (`""` / `[]`) — identical to the pre-tri-state behavior for every caller.

**`updateCalendarEvent` does not own `startTime`, `endTime`, `timezone` or
`rruleString`.** Those stay on `rescheduleCalendarEvent` — the two mutations are
deliberately non-overlapping, and `updateCalendarEvent` always re-passes the event's
current time/timezone/recurrence-rule fields unchanged.

Owner-scoped tokens may only update events on calendars their owner owns; a
cross-owner `eventId` returns the same `"Event not found."` error a genuinely missing
event would, so existence is never leaked to a caller outside that scope.

Identifier writes on this mutation share `scheduleEvent`'s validation and reject (with
the whole update rolled back) on: an invalid `system` URL; a blank/whitespace-only or
over-255-character `identifier`; a `(system, identifier)` pair already claimed by
another record of the same type in the organization; or two pairs in one payload that
normalize to the same `system`. (Two of the six identifier domain errors — an
out-of-allowlist target and a cross-organization target — can never be reached from a
caller-supplied body on either mutation, since the target and organization are always
resolved server-side.)

## RSVP statuses

`RSVPStatus` (`accepted`, `declined`, `pending`) is shared between
`EventAttendance`, `EventExternalAttendance`, and `ResourceAllocation`.
A few practical patterns:

- **Auto-accept for resources** is *not* the default — schedulers may
  want manual confirmation that a costly resource (OR, MRI suite) is
  allocated.
- **Patient RSVP** is typically tracked on `EventExternalAttendance` —
  "patient confirmed the visit" updates this row.
- **Provider RSVP**: physicians' acceptance comes through their
  `EventAttendance`, often synced from the provider's reply on
  Google/Outlook.
