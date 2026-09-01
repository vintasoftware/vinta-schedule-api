# API changes: Calendar Pools Phase 1 — lenient roster removal

- **Date:** 2026-09-01
- **Scope:** `plan/calendar-pools/phase-1` vs `main` (based on `plan/calendar-pools/phase-0`, `78bee1d3`)
- **Audience:** Web SPA (React), Partner integrations
- **Breaking changes:** 0 (this phase only *removes* a rejection and a deletion — see **Behavior changes** below for why a client can still be affected)

## Summary

Editing a `CalendarGroup`'s slots (`PATCH`/`PUT /calendar-groups/{id}/` on REST,
`updateCalendarGroup` on GraphQL) used to **refuse** to drop a calendar from a
slot's roster whenever that calendar still had a future-booked appointment
through the group, and — when the removal was allowed — it **deleted** that
calendar's group-scoped availability windows, blocked time, and quota rules
for the slot. As of this phase, removing a calendar from a slot's roster
**always succeeds**: existing bookings keep their calendar selections exactly
as they were, past and future, and the removed calendar's group-scoped
configuration is kept and keeps being enforced (e.g. on a later reschedule of
one of those bookings). No request shape changed and no field was added,
removed, or renamed — this is a pure behavior change on an existing operation.

## Behavior changes

### `PATCH`/`PUT /calendar-groups/{id}/` and `updateCalendarGroup` — roster removal no longer rejected

- **Status:** changed (not breaking — this removes a rejection; every request
  shape and success-response shape is unchanged).
- **Auth:** unchanged (REST: `CalendarGroupPermission`, admin or the group's
  owning members; GraphQL: `updateCalendarGroup` mutation, same org-scoped
  auth as before).

**Before this phase:** if the submitted `slots[].calendar_ids` for an existing
slot omitted a calendar that still had a `CalendarEventGroupSelection` on a
**future**-starting event booked through that group, the whole update was
rejected:

- REST: `400 Bad Request`, body `{"non_field_errors": ["Cannot remove slot or
  calendar because it is referenced by future group bookings."]}`.
- GraphQL: `updateCalendarGroup` resolved successfully (HTTP 200, no
  `errors[]` — this API returns failures as data) with
  `{"success": false, "group": null, "errorMessage": "Cannot remove slot or
  calendar because it is referenced by future group bookings."}`.

The same call also used to **delete** that calendar's group-scoped
`AvailableTime` (custom availability windows), `BlockedTime`, and quota-rule
rows for that slot, whenever the removal *was* allowed (i.e. no future
booking existed yet).

**After this phase:** the same request **succeeds**. The response is the
updated group with the calendar no longer listed in that slot's `calendars` —
identical shape to any other successful update, just no longer conditioned on
whether the calendar has future bookings. Any event that already selected the
removed calendar keeps that selection (visible unchanged on
`CalendarEventGroupSelection` / wherever a client reads an event's group
selections — no new field marks a selection as "stale" yet; see **Rollout**
below). The calendar's group-scoped availability windows, blocked time, and
quota rules are **no longer deleted** — they stay in place and keep being
enforced, including on a later reschedule of one of the grandfathered
bookings, and reappear "active" again if the same calendar is re-added to the
roster later.

**Removing a whole slot** (a slot name absent from the submitted `slots[]`
list, as opposed to a calendar being dropped from a slot that still exists)
is **still refused** when the slot has a future-booked event, but the
message text **changed**: it is now `"Cannot remove slot because it is
referenced by future group bookings."`, narrowed from the previous
`"Cannot remove slot or calendar because it is referenced by future group
bookings."` (the "or calendar" half no longer applies, since calendar
removal from a surviving slot is never refused). Any client doing an
exact-string match on the old message — for slot removal, or for detecting
this error generically — must update to the new string. This distinction
did not exist as a client-visible split before (both cases shared one error
path); it exists now only because slot removal cascades and drops the
slot's own group-scoped configuration with it, which is a different action
from removing one calendar from a surviving slot's roster.

**Booking creation is unchanged.** Creating a grouped event
(`POST /calendar-groups/{id}/create-event/`-style action / `createGroupEvent`,
whichever your platform calls it) still rejects any selected calendar that is
not currently in its slot's roster, exactly as before.

#### Example — REST, removing a calendar with a future booking

Request:

```http
PATCH /calendar-groups/42/ HTTP/1.1
Content-Type: application/json

{
  "name": "Clinic Appointments",
  "description": "",
  "slots": [
    {
      "id": 101,
      "name": "Physicians",
      "calendar_ids": [77],
      "required_count": 1,
      "order": 0
    }
  ]
}
```

(`calendar_ids` previously included `55` — a physician with a booking next
week through this slot — and no longer does.)

**Before this phase** — `400 Bad Request`:

```json
{
  "non_field_errors": [
    "Cannot remove slot or calendar because it is referenced by future group bookings."
  ]
}
```

**After this phase** — `200 OK`:

```json
{
  "id": 42,
  "name": "Clinic Appointments",
  "description": "",
  "slots": [
    {
      "id": 101,
      "name": "Physicians",
      "description": "",
      "order": 0,
      "required_count": 1,
      "calendars": [
        { "id": 77, "name": "Dr. B", "external_id": "phys_b", "...": "..." }
      ]
    }
  ],
  "created": "2026-08-01T12:00:00Z",
  "modified": "2026-09-01T20:00:00Z"
}
```

The future-booked event that had calendar `55` selected for slot `101` is
untouched: its `CalendarEventGroupSelection` for `(event, slot 101, calendar
55)` still exists, even though calendar `55` no longer appears in slot `101`'s
`calendars`.

#### Example — GraphQL, same scenario

```graphql
mutation {
  updateCalendarGroup(
    input: {
      organizationId: 7
      groupId: 42
      name: "Clinic Appointments"
      slots: [
        { name: "Physicians", calendarIds: [77], requiredCount: 1, order: 0 }
      ]
    }
  ) {
    success
    errorMessage
    group {
      id
      slots {
        name
        calendars {
          id
        }
      }
    }
  }
}
```

**Before this phase:**

```json
{
  "data": {
    "updateCalendarGroup": {
      "success": false,
      "errorMessage": "Cannot remove slot or calendar because it is referenced by future group bookings.",
      "group": null
    }
  }
}
```

**After this phase:**

```json
{
  "data": {
    "updateCalendarGroup": {
      "success": true,
      "errorMessage": null,
      "group": {
        "id": 42,
        "slots": [{ "name": "Physicians", "calendars": [{ "id": 77 }] }]
      }
    }
  }
}
```

### Client migration notes

- **Web SPA (React):** if the roster-editing UI currently surfaces
  `non_field_errors` (REST) or `errorMessage` (GraphQL) containing "referenced
  by future group bookings" as a blocking validation message when an admin
  drops a calendar from a slot, that message will stop appearing for calendar
  removal (it can still appear for removing a whole slot). No code change is
  required for the request to keep working — the request shape is identical —
  but any UI copy, disabled-button logic, or confirmation dialog keyed
  specifically on that error string for the *calendar-removal* case should be
  removed or revisited, since the operation now always succeeds. Consider
  adding a soft warning instead ("removing this calendar will not affect
  already-booked appointments") since the backend no longer blocks the action
  or tells the client how many future bookings are affected.
- **Partner integrations:** any integration that relied on the rejection as a
  guard (e.g. "try to remove the calendar; if it fails, tell the ops user
  there are pending appointments") must replace that check with its own query
  against events/selections if it still needs to warn about affected
  bookings — the backend will not surface that count on this call anymore.
  Idempotent retry logic that previously treated the 400/`errorMessage` as a
  terminal state for that calendar should be revisited, since a retried call
  now succeeds.
- Both platforms: a call that used to be rejected and is retried unmodified
  after this deploy will now succeed instead of failing. If any client-side
  code specifically asserts on the old rejection (e.g. integration tests
  against a fixture that has a future booking), that assertion needs to be
  updated to expect success.

### Re-adding a calendar to a roster restores its previous quota rules — a second `POST` for the same period now fails

Because a departed calendar's `CalendarGroupSlotQuotaRule` rows are no longer
deleted on removal (see **Billing / limits** below for the availability-window
equivalent), re-adding that calendar to the same slot brings its old quota
rules back exactly as they were — cap and period included. This is new: a
client that used to treat "remove calendar, re-add calendar" as returning the
slot to a clean state, then `POST`ed a fresh quota rule for a period the
calendar already had configured (e.g. one `DAY` rule per calendar is a common
UI default), will now get a `400`/`CalendarGroupValidationError`
(`"A quota rule for period 'day' already exists for this calendar and
slot."`) instead of a `201` — the same uniqueness constraint that always
existed on `(group_slot, calendar, period)`, just newly reachable because the
old rule survives the round trip instead of being deleted with the
membership. Any UI or integration flow that creates a default quota rule
after adding a calendar back to a roster should check for an existing rule
first (or use the update/`PATCH` path on the existing rule) rather than
unconditionally `POST`ing a new one.

## Billing / limits

Removing a calendar from a slot's roster no longer frees `availability_windows`
capacity. That calendar's group-scoped `AvailableTime` (and `BlockedTime`) rows
are counted by the `availability_windows` metered resource regardless of
whether the calendar is currently in a roster (see
`payments/seams/resources.py`'s `_count_availability_windows`), and — per the
behavior change above — those rows are no longer deleted when the calendar
leaves a slot. Before this phase, removing a calendar from a roster deleted
its group-scoped windows and the organization's metered count dropped with
it; after this phase, the rows and the count both survive the removal
unchanged. An organization operating at or near its `availability_windows`
ceiling will **not** free capacity by editing a group's roster — it must
explicitly delete the group-scoped windows/blocks it no longer needs (e.g. via
`delete_group_scoped_availability_window` / `delete_group_scoped_blocked_time`)
if it wants that capacity back. This is a behavior change worth flagging to
any client-side "manage your plan" UI that told users removing a calendar
would reduce their usage.

## Other contract changes

None. No new endpoints, fields, error codes, or auth changes in this phase.
Staleness surfacing (a computed flag on `CalendarEventGroupSelection` marking
whether its calendar is still in the slot's current roster, plus a
sweep query for ops) ships in the next phase of this plan and will be
documented separately when it lands.

## Rollout

Live on deploy of `plan/calendar-pools/phase-1` — no feature flag (this phase
took an explicit no-flag waiver: the change is strictly less destructive than
today's behavior, so there is nothing to roll forward incrementally, and the
rollback path is a plain code revert). No environment sequencing beyond the
normal deploy pipeline.
