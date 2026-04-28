# Calendar Groups, Slots, and Slot Selections

> Source: [calendar_integration/models.py](../../calendar_integration/models.py) — `CalendarGroup`, `CalendarGroupSlot`, `CalendarGroupSlotMembership`, `CalendarEventGroupSelection`, `CalendarEvent.calendar_group`.
> Service: [calendar_integration/services/calendar_group_service.py](../../calendar_integration/services/calendar_group_service.py).
> Plan: [dev-plans/2026-04-20-CALENDAR_GROUP_PLAN.md](../../dev-plans/2026-04-20-CALENDAR_GROUP_PLAN.md).

A **`CalendarGroup`** is a **booking template**. It says: "to book this
kind of appointment, the caller must provide one (or more) calendar
from each of these named slots, and they all have to be free at the
same time."

It is the right primitive whenever a booking needs to combine
calendars that are **picked from a pool** at booking time — which is
the dominant pattern in clinical scheduling.

Compared to a [bundle](calendar-bundles.md): bundles fix the membership
("always Dr. Lee + this one room"); groups fix the **roles** and let
you pick from a pool per role ("any cardiologist + any cath lab").

## Anatomy

```
CalendarGroup  "Cardiology Outpatient"
│
├── CalendarGroupSlot  name="Physicians"   order=0  required_count=1
│       └─ CalendarGroupSlotMembership rows (the pool):
│            • Dr. Lee
│            • Dr. Patel
│            • Dr. Okafor
│
├── CalendarGroupSlot  name="Rooms"        order=1  required_count=1
│       └─ Pool: { Exam Room 3, Exam Room 4, Exam Room 5 }
│
└── CalendarGroupSlot  name="Nurses"       order=2  required_count=2
        └─ Pool: { Nurse Ana, Nurse Ben, Nurse Cho, Nurse Dan }
```

Booking on this group requires the caller to pick:

- **1** physician calendar from the Physicians pool,
- **1** room calendar from the Rooms pool,
- **2** nurse calendars from the Nurses pool.

…and the system will only allow the booking if every picked calendar is
free for the requested time window.

## Models

### `CalendarGroup`

The template aggregate. Per-organization unique by `name`. Has many
`CalendarGroupSlot`s (its slots) and many `CalendarEvent`s (the
bookings made through it).

### `CalendarGroupSlot`

A required role inside a group. Carries:

- `name` — human-meaningful ("Physicians", "Rooms", "Nurses").
- `order` — display/iteration order. The *lowest-order* slot is by
  convention the **primary** slot for booking (its first selection
  becomes the event's primary calendar).
- `required_count` — how many calendars from the pool must be picked at
  booking time (default `1`; `2` for the "two nurses" example above).
- `calendars` — many-to-many to `Calendar` through
  `CalendarGroupSlotMembership`. This is the **pool**.

### `CalendarGroupSlotMembership`

Through table linking a `Calendar` to a `CalendarGroupSlot`. One row
per (slot, calendar). A given calendar may belong to multiple slots
across the same or different groups.

### `CalendarEventGroupSelection`

A row per (event, slot, calendar) recording **which calendars satisfied
which slot** for a particular booking. When `CalendarEvent.calendar_group`
is non-null, the event has one or more selection rows describing the
picks. The unique constraint enforces no duplicate (event, slot,
calendar) tuples.

`CalendarEvent.calendar_fk` (the event's "primary" calendar) is also
recorded inside the selections — it's the picked calendar from the
lowest-`order` slot.

## Booking semantics — `CalendarGroupService.create_grouped_event`

The service's `create_grouped_event` method:

1. Validates that every slot has a selection of `>= required_count`
   calendars, all from that slot's pool, with no duplicates.
2. Validates every selected calendar is **available** for the requested
   `(start_time, end_time)` via
   `Calendar.objects.only_calendars_available_in_ranges`. The whole
   booking is rejected if even one calendar is busy.
3. Picks the **primary calendar** = the first selection of the
   lowest-`order` slot.
4. Delegates to `CalendarService.create_event` on the primary calendar
   so existing side-effects (external-provider sync, permissions,
   attendee invites) all run unchanged.
5. Persists `CalendarEventGroupSelection` rows for every (slot,
   calendar) pick.
6. Creates a `BlockedTime` on every non-primary selected calendar so
   they appear busy. The service skips the `BlockedTime` for cases
   where an external-provider invite will reliably create an equivalent
   event natively (same provider on both sides, with an attendee link).

## Availability and bookable slots

- `CalendarGroupQuerySet.only_groups_bookable_in_ranges(ranges)` —
  returns groups where every slot has at least `required_count`
  available calendars for *every* range. Use when listing which groups
  a patient can book against.
- `CalendarGroupService.check_group_availability(group_id, ranges)` —
  for one group, returns per-range, per-slot lists of which pool
  calendars are available. Use when rendering "who's free?" UIs.
- `CalendarGroupService.find_bookable_slots(group_id, search_window,
  duration, slot_step)` — walks the search window in fixed steps and
  returns timestamps where every slot is satisfiable. Use to drive a
  "show me bookable times this week" picker.

## Healthcare examples

### Example A — Outpatient clinic appointment

The driving example. A physician + a room.

- Group: `"Cardiology Outpatient"`.
- Slot 1 — `Physicians`, pool of 4 cardiology personal calendars,
  `required_count=1`.
- Slot 2 — `Rooms`, pool of 3 exam-room resource calendars,
  `required_count=1`.

When a patient books a 2:30 PM Tuesday consult:

- The system finds physicians free at 2:30 PM and rooms free at 2:30 PM
  and lets the patient (or scheduler) pick.
- The chosen physician's personal calendar gets the canonical event;
  the chosen room gets a `BlockedTime`.

### Example B — Surgery scheduling

Surgery for a patient typically needs **a surgeon, an
anaesthesiologist, an OR, and a circulating nurse**. The pools are
fluid: any qualified surgeon, any of the OR's matching that surgeon's
specialty, etc.

- Group: `"General Surgery"`.
- Slots: `Surgeons` (req 1), `Anaesthesiologists` (req 1), `ORs`
  (req 1), `Scrub Nurse` (req 1), `Circulating Nurse` (req 1).
- A booking flow searches for a window where one calendar in each pool
  is free — exactly what `find_bookable_slots` does. Once a window is
  picked, the user (or an algorithm) chooses one calendar from each
  slot.

### Example C — On-call shift assignment

A hospitalist on-call shift covers Friday 7 PM – Saturday 7 AM. The
shift is satisfied by **one hospitalist** picked from a roster pool.

- Group: `"Overnight Hospitalist On-Call"`.
- Slot: `Hospitalist`, pool = the hospitalist personal calendars,
  `required_count=1`.

The "pick any free one from the pool" semantics make `CalendarGroup`
useful even for single-resource bookings where the caller wants a
generic "anyone" affordance instead of pre-selecting.

### Example D — Multi-disciplinary clinic visit

A complex chronic-care visit might require:

- One PCP, one specialist, one care coordinator, **two** medical
  assistants (chart prep + vitals), one large consult room.

That's five slots, with `required_count=2` on the medical-assistants
slot — exactly what the model is shaped to do.

### Example E — Telehealth with a virtual interpreter

For a telehealth visit that needs a third-party interpreter:

- Slot 1 — `Physicians` (pool of physicians on the platform),
  `required_count=1`.
- Slot 2 — `Interpreters` (pool of interpreter virtual calendars filtered
  by language), `required_count=1`.
- Slot 3 — `Telehealth Endpoints` (pool of virtual room calendars),
  `required_count=1`.

The same machinery handles purely-virtual bookings.

## Updating and deleting groups

- `update_group` reconciles slots and pool memberships against the
  incoming spec. Slots missing from the incoming data are deleted, but
  the service refuses to drop a slot, or evict a calendar from a
  slot's pool, if a **future** booking selects that calendar in that
  slot — to avoid orphaning live appointments.
- `delete_group` refuses if there are any past or future events
  referencing the group (the FK from `CalendarEvent` is `PROTECT`).

## Why slots have `required_count`

Most slots are 1-of-N ("one cardiologist"), but a few need N-of-M:

- "Two nurses" for procedural conscious sedation.
- "Two attendings + one fellow" for some teaching hospital procedures.
- "Two interpreters" for long telehealth sessions where a relay is
  needed.

`required_count` lets the same model express both without a special
case. The pool size must always be at least `required_count`.

## Relationship to other concepts

- A grouped event is still a **`CalendarEvent`** — recurrence,
  attendances, external attendees, and resource allocations all work as
  documented in [events.md](events.md) and [recurrence.md](recurrence.md).
- A bundled event is a different mechanism with a fixed membership and
  a single primary calendar — see
  [calendar-bundles.md](calendar-bundles.md). New flows that need
  per-role pool picking should use groups; old flows that already work
  through bundles can keep doing so.
- Group availability ultimately resolves to per-calendar availability,
  computed via `only_calendars_available_in_ranges` — see
  [availability.md](availability.md). Improvements to that method
  automatically improve group bookability.
