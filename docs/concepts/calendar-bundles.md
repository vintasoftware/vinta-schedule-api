# Calendar Bundles

> Source: [calendar_integration/models.py](../../calendar_integration/models.py) — `Calendar` (with `calendar_type=BUNDLE`), `ChildrenCalendarRelationship`, `CalendarEvent.bundle_calendar`, `CalendarEvent.bundle_primary_event`, `BlockedTime.bundle_calendar`.
> Service: `CalendarService.create_bundle_calendar` and `_create_bundle_event` in [calendar_integration/services/calendar_service.py](../../calendar_integration/services/calendar_service.py).

A **bundle calendar** is a `Calendar` with `calendar_type=BUNDLE` that
acts as a single bookable façade for a *fixed* set of underlying
calendars (its **child calendars**). When something is booked on the
bundle, the service creates the canonical event on a designated
**primary** child and propagates a representation (event or
`BlockedTime`) onto every other child.

Use a bundle when:

- You always need the **same group** of calendars together for every
  booking.
- You want **one** authoritative event in an external provider
  (Google/Microsoft) and the other calendars to merely reflect "busy."
- You don't need to *pick* among candidates at booking time — the
  members are fixed.

If any of those don't hold (you want to pick a physician from a pool, or
the booking should be free of one canonical primary), prefer a
[**CalendarGroup**](calendar-groups.md) instead.

## Anatomy

```
                                         ┌─────────────────────────────────────┐
       BUNDLE Calendar                    │ ChildrenCalendarRelationship rows  │
   ─────────────────────────              │   bundle_calendar=<bundle>          │
   "Cardiology Procedure Suite"   ──────▶ │   child_calendar=<Dr. Lee>          │
                                          │     is_primary=True                  │
                                          ├─────────────────────────────────────┤
                                          │   child_calendar=<Cath Lab 1>       │
                                          │     is_primary=False                 │
                                          ├─────────────────────────────────────┤
                                          │   child_calendar=<Cardiac Tech>     │
                                          │     is_primary=False                 │
                                          └─────────────────────────────────────┘
```

`ChildrenCalendarRelationship` is the through table; exactly one row per
bundle has `is_primary=True`.

## Booking flow

`CalendarService._create_bundle_event` walks roughly this path:

1. **Availability check** for *every* child calendar over the requested
   time window. If any child is unavailable, the booking fails — the
   bundle's contract is "everyone or no-one."
2. **Pick the primary calendar** (the child marked `is_primary=True`).
   This is the calendar that owns the canonical event in its external
   provider.
3. **Collect attendees**: the explicit attendances on the input plus
   all owners of every child calendar. The owners get added so they
   receive the invite on their own provider calendar (not just a local
   `BlockedTime`).
4. **Create the primary event** through `CalendarService.create_event`
   so all the normal side-effects run (provider sync, notifications,
   permissions).
5. **For each non-primary child**:
   - If the child is `INTERNAL`, create a full `CalendarEvent`
     representation linked back via `bundle_primary_event`.
   - Otherwise, create a `BlockedTime` with
     `bundle_primary_event=<primary>` and
     `bundle_calendar=<bundle>` so the child shows as busy without
     duplicating event details (and without polluting the upstream
     provider calendar).

## Healthcare examples

### Example A — A cath-lab procedure

A cardiac catheterisation requires the cardiologist, the cath lab, and a
cardiac tech together every time. They never substitute.

- Bundle: `"Cardiology Procedure Suite"`.
- Children: `Dr. Lee` (personal, Google), `Cath Lab 1` (resource,
  internal), `Cardiac Tech` (personal, Microsoft).
- Primary: `Dr. Lee` (so the procedure shows up natively on her Google
  calendar, with the lab + tech as invitees / co-busy).

Booking a procedure on the bundle:

- Creates one `CalendarEvent` on Dr. Lee's calendar (synced to Google,
  attendees include the tech).
- Creates a `BlockedTime` on `Cath Lab 1` (internal, but represented as
  "busy" rather than as a full event the lab "owns").
- Creates a `BlockedTime` on the tech's calendar via Microsoft (since
  Microsoft and Google are different providers, the cross-provider
  invite isn't guaranteed and we want a local source of truth).

### Example B — Surgical theatre standing config

A specific surgical configuration always requires:

- One surgeon (Dr. Okafor).
- One anaesthesiologist (Dr. Reyes).
- OR-2.
- The robotic surgery cart.

Same shape: a bundle with four children, primary = the surgeon. Every
booking blocks all four.

### Example C — Group-therapy room + facilitator

A weekly recurring group therapy session uses Therapist Maya + Group
Room B together. The bundle sits behind a public scheduling URL that
patients use to enrol — but enrolment never reassigns to a different
therapist or room, so a fixed bundle is the right model.

## Recurrence on bundles

Bundle events support recurrence: passing `recurrence_rule` to the
booking flow creates a recurring primary event and recurring
representations/blocked times on the children. Cancelling a single
occurrence (e.g. one week's group therapy because the therapist is
sick) flows through the standard recurrence-exception machinery on
the primary; the children's representations follow the primary.

## Why groups exist on top of bundles

Bundles answer "always book these N calendars together." They don't
answer "pick any one of these physicians and any one of these rooms."
That's the case `CalendarGroup` handles — see
[calendar-groups.md](calendar-groups.md). For new flows where the
caller picks calendars at booking time, prefer groups; bundles are the
right call when the membership is fixed and you want a single primary
calendar that owns the provider sync.
