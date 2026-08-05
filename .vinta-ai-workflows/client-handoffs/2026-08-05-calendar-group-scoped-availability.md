# API changes: Calendar Group-Scoped Availability

- **Date:** 2026-08-05
- **Scope:** `feat/calendar-group-scoped-availability` (Phases 0–3c) vs `main` (`ed63753..b9b9ae8`)
- **Audience:** Web SPA (React), Partner integrations
- **Breaking changes:** none in the traditional sense — one **behavior change** for existing
  integrations that author blocked time (see below). No existing operation's request/response
  shape, route, or auth requirement changed.

## Summary

This branch adds a new concept — **group-scoped availability** — on top of the existing
calendar-group booking feature. Within a group's slot, an admin (or a calendar's owner) can now:

1. **Narrow** a calendar's bookable hours with **group-scoped availability windows** (intersect
   only — never widens the calendar's base availability).
2. **Block out** time for a calendar within that group with **group-scoped blocked time**.
3. **Cap** how many bookings a calendar can take through that group in a period with **quota
   rules** (day / week / month).

All three are additive: a group slot with none of this configuration behaves exactly as it did
before this branch (byte-identical output, same query counts). Six new REST resource families
and six new public GraphQL operations are documented below, one per concept per surface.

**The one thing that changes for existing integrations without any action on their part:**
starting with this branch, **all blocked time (not only the new group-scoped kind) counts toward
the `availability_windows` plan limit.** See **Breaking changes** below.

## Breaking changes

### 1. Blocked-time metering now includes ALL blocked time, not only availability windows

Before this branch, the `availability_windows` plan-limit counter summed only user-authored
**availability windows**. As of this branch it sums user-authored availability windows **plus**
user-authored **blocked time** — both the pre-existing base blocked time and the new group-scoped
kind.

- **What breaks:** nothing throws or 4xxs differently on its own. What changes is **reported
  usage**: any organization that has authored blocked time (via `createBlockedTime` /
  `updateBlockedTime`, the REST `blocked-times/` endpoints, or bulk blocked-time writes) will see
  its `availability_windows` usage figure rise the next time it's read (dashboard, the usage
  field on subscription/entitlement queries, or a `limit_exceeded` error body's `current_usage`).
  An organization sitting close to its plan ceiling may now receive **HTTP 402 / `OverLimitError`
  (`code: "limit_exceeded"`, `resource: "availability_windows"`)** on a `createAvailabilityWindow`,
  `batchUpdateAvailabilityWindows`, or the new group-scoped window writes where it previously
  succeeded, purely because its blocked-time count now contributes to the same ceiling.
- **Why it's safe to ship now:** the product is pre-customer, so no organization's usage jumps
  against a live account today. It is called out here so client teams build their usage/limit
  UI against the new (correct, going-forward) semantics rather than the old ones.
- **What to do:** if you display "X of Y availability windows used" anywhere, treat the count as
  "windows + blocked time" going forward, and if you pre-flight-check remaining quota before a
  bulk blocked-time write, account for it consuming the same budget an availability window would.

### 2. Existing availability operations are unchanged (documented for confirmation, not as a break)

The existing availability read/write contract — `availabilityWindows` / `unavailableWindows`
queries, `createAvailabilityWindow`, `updateAvailabilityWindow`, `deleteAvailabilityWindow`,
`batchUpdateAvailabilityWindows`, and the REST `available-times/` / `blocked-times/` (top-level,
non-group) endpoints — is **frozen**: no field added, removed, renamed, or retyped; no auth
change; no error-shape change. Every phase that could plausibly touch it carries a regression
test asserting byte-identical output and unchanged query counts. Nothing to change on the client
side for these operations.

## New concepts and v1 limitations

- **Intersect-only.** A group-scoped availability window can only narrow a calendar's existing
  base availability, never extend it. A calendar with no base availability at a given time is
  never bookable there, regardless of group-scoped configuration.
- **Quota periods are measured in UTC**, not the calendar's local timezone. A "1 per day" rule's
  day boundary is midnight UTC, not midnight in the calendar owner's timezone. This is a
  documented v1 simplification, not a bug — plan UI copy accordingly if you display period
  boundaries to end users.
- **Rescheduling is enforced identically to initial booking.** A reschedule request is checked
  against group-scoped windows, blocks, and quota for **every calendar selected in the group
  booking**, not only the calendar being moved.
- **Non-disclosure.** Every route/operation below returns the exact same "not found" shape
  whether a group/slot/window/block/rule genuinely doesn't exist, belongs to another
  organization, or the caller simply isn't authorized to see it. Do not attempt to distinguish
  "doesn't exist" from "you can't see it" from these responses — the API deliberately does not
  let you.
- **Orphaned-booking warning.** Windows and blocks can *narrow* a calendar out from under
  confirmed future bookings. The create (when it's the calendar's first group-scoped window) and
  every update of a window or block returns an `orphaned_bookings` list identifying any confirmed
  future bookings in that group that no longer fit. **Nothing about those bookings is changed or
  cancelled automatically** — this is purely a heads-up for the caller to act on manually (e.g.
  notify the attendee, rebook, or cancel by hand). Quota rules have no equivalent field: a quota
  rule caps *future* bookings only and can never orphan an existing one.

---

## Booking/reschedule rejection shape

When a directly-named calendar in a group booking or reschedule violates a group-scoped rule, the
request is rejected. The rejection identifies the **calendar** and the **rule type violated** —
`OUTSIDE_WINDOW`, `INSIDE_BLOCK`, or `QUOTA_CONSUMED` — and deliberately **never includes the
configured values** (the window's hours, the block's reason, or the quota's cap/current count).
This is enough for an org admin to act on, without leaking roster/scheduling detail to an
external/anonymous booker on a public booking link.

Evaluation order when a calendar violates more than one rule: **block beats window** (an
in-block time is rejected as `INSIDE_BLOCK` even if it's also outside a window), and **quota is
checked last**, after both window and block checks pass.

How this surfaces differs by which mutation you're calling:

- **Authenticated, internal booking flows** — REST `POST /calendar-groups/{id}/events/` and the
  GraphQL `rescheduleCalendarGroupEvent` mutation — surface the calendar id and rule type in the
  error message text:
  - REST: **400** with body
    ```json
    { "non_field_errors": ["Calendar 42 is not bookable for the requested time in this group (outside_window)."] }
    ```
  - GraphQL: a standard `errors[].message` string with the same text, e.g.
    `"Calendar 42 is not bookable for the requested time in this group (inside_block)."`
    (rule-type tokens: `outside_window`, `inside_block`, `quota_consumed`).
- **Public, code-based booking/reschedule flows** — `createCalendarGroupEventWithCode`,
  `rescheduleCalendarGroupEventWithCode`, and the equivalent cancellation-code mutations — collapse
  ANY group-scoped rule violation into the same generic bucket already used for "outside base
  availability": `errorCode: "SLOT_UNAVAILABLE"`, `errorMessage: "The requested time slot is not
  available."` No calendar id, no rule type, no distinction from an ordinary base-availability
  miss. The booking code is **not consumed** on this rejection, so the caller can retry with a
  different time.

---

## 1. Group-scoped availability windows

### REST — `.../calendar-groups/{group_id}/slots/{slot_id}/availability-windows/`

- **Status:** added — not breaking.
- **Auth:** session/JWT (`jwtAuth` or `cookieAuth`), organization-scoped. Optional
  `X-Organization-Id` header (required only if the caller has 2+ active memberships). Route
  visibility requires the caller to be able to see the group (org admin, or owner of any
  calendar in any slot of the group); the per-write authorization additionally requires the
  caller to own the specific calendar being configured (or be an org admin). A caller who fails
  either check gets the same 404 as a genuinely missing group/slot.
- **Methods:** `GET` (list), `POST` (create), `GET /{id}/` (retrieve), `PATCH /{id}/` (partial
  update — **PUT is not supported**), `DELETE /{id}/`.

**List** (`GET`) — paginated (`limit`/`offset` query params), returns
`{ count, next, previous, results: GroupScopedAvailabilityWindow[] }`.

**`GroupScopedAvailabilityWindow` (read shape):**

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `calendar_id` | int | |
| `group_slot_id` | int | |
| `start_time` | datetime (ISO 8601) | rendered in the window's own IANA timezone, not UTC |
| `end_time` | datetime (ISO 8601) | same |
| `timezone` | string | IANA timezone name |
| `rrule_string` | string \| null | `null` when the window is a one-off, not recurring |
| `is_recurring` | bool | |
| `created` | datetime | |
| `modified` | datetime | |

**Create** (`POST`) — request body:

```json
{
  "calendar": 42,
  "start_time": "2026-09-01T09:00:00Z",
  "end_time": "2026-09-01T17:00:00Z",
  "timezone": "America/Sao_Paulo",
  "rrule_string": "RRULE:FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR"
}
```

- `calendar` (int, **required**) — must be a member of the target slot's roster, or the write is
  rejected with the non-disclosure 404.
- `start_time`, `end_time` (datetime, **required**) — `start_time` must be before `end_time`.
- `timezone` (string, **required**).
- `rrule_string` (string, optional) — omit for a one-off window.

Response: **201** with a `GroupScopedAvailabilityWriteResult`:

```json
{
  "window": { "id": 501, "calendar_id": 42, "group_slot_id": 7, "start_time": "...", "end_time": "...", "timezone": "America/Sao_Paulo", "rrule_string": "RRULE:...", "is_recurring": true, "created": "...", "modified": "..." },
  "orphaned_bookings": [
    { "id": 9001, "calendar_id": 42, "title": "Checkup", "start_time": "2026-09-05T20:00:00-03:00", "end_time": "2026-09-05T20:30:00-03:00" }
  ]
}
```

`orphaned_bookings` is populated only when this is the calendar's **first** group-scoped window
in the slot (i.e. the write is what narrows it away from unrestricted base availability) and
there are confirmed future bookings that now fall outside it. Otherwise it's `[]`.

**Update** (`PATCH /{id}/`) — every field optional, only provided fields change. `rrule_string`
is **tri-state**: omit it to leave recurrence unchanged, send `null` to clear it (make
non-recurring), send a string to set/replace it. Response: **200** with the same
`GroupScopedAvailabilityWriteResult` shape as create; `orphaned_bookings` is populated whenever
the update narrows the effective window (not only on the very first write).

**Delete** (`DELETE /{id}/`) — deletes the whole series if recurring. Response: **204**, no body.

**Errors:**
- **404** (`{"detail": "Not found."}`) — window doesn't exist, belongs to another organization,
  is outside the slot named in the URL, OR the caller isn't authorized to manage it. All four
  cases are byte-identical.
- **400** — `start_time >= end_time`, or other field validation.

### Public API (GraphQL) — `groupScopedAvailabilityWindows` query

```graphql
query {
  groupScopedAvailabilityWindows(groupSlotId: 7, calendarId: 42, offset: 0, limit: 100) {
    id calendarId groupSlotId startTime endTime timezone rruleString isRecurring created modified
  }
}
```

- **Auth:** `Authorization: Bearer <systemUserId>:<token>`. Requires the
  `GROUP_SCOPED_AVAILABILITY_WINDOWS` resource grant on the token. For a calendar-owner-scoped
  token, results are filtered to windows on calendars in the token's owner scope automatically.
- **Args:** `groupSlotId` (int, required), `calendarId` (int, optional — filters to one calendar),
  `offset` (int, default 0), `limit` (int, default 100).
- **Returns:** `[GroupScopedAvailabilityWindowGraphQLType]` — same fields as the REST read shape
  above, camelCase. Raw rows (one per recurring master or one-off window), not expanded
  occurrences.

### Public API (GraphQL) — `batchUpsertGroupScopedAvailabilityWindows` mutation

```graphql
mutation {
  batchUpsertGroupScopedAvailabilityWindows(input: {
    organizationId: 1
    groupSlotId: 7
    operations: [
      { action: "create", calendarId: 42, startTime: "2026-09-01T09:00:00Z", endTime: "2026-09-01T17:00:00Z", timezone: "America/Sao_Paulo" }
      { action: "update", calendarId: 42, windowId: 501, endTime: "2026-09-01T18:00:00Z" }
      { action: "delete", calendarId: 43, windowId: 502 }
    ]
  }) {
    success
    errorMessage
    windows { id calendarId groupSlotId startTime endTime timezone rruleString isRecurring created modified }
  }
}
```

- **Auth:** requires the `BATCH_UPSERT_GROUP_SCOPED_AVAILABILITY_WINDOWS` resource grant. Every
  operation's `calendarId` must be within the token's owner scope (checked per-operation before
  any write) — an org-wide token has no such restriction.
- **`GroupScopedAvailabilityWindowOperationInput` fields:** `action` (`"create" | "update" |
  "delete"`, required), `calendarId` (int, **required on every op**, not only create),
  `windowId` (int, required for update/delete), `startTime`/`endTime`/`timezone` (required for
  create, optional for update — only provided fields change), `rruleString` (optional).
- **Semantics — all-or-nothing:** the whole batch runs in one transaction; every operation is
  validated and every referenced row resolved before anything is written. Any failure (bad id,
  IDOR mismatch, over-limit) rolls back the entire batch — nothing is partially applied.
- **Idempotent replay:** a `create` op with content identical to an existing group-scoped window
  (same calendar, slot, start/end, timezone, rrule) is a no-op — it returns the existing window
  unchanged, does not duplicate it, and is not charged against the plan limit. Replaying the same
  batch after a network timeout is safe.
- **Over-limit (metered):** windows are metered. If the batch's **net** growth (genuine creates
  minus deletes of counted rows) would exceed the organization's `availability_windows` plan
  limit, the entire batch is rejected. Rendered as a GraphQL error whose `extensions` carry the
  shared over-limit body:
  ```json
  {
    "detail": "Organization is at its limit for availability windows.",
    "code": "limit_exceeded",
    "resource": "availability_windows",
    "current_usage": 50,
    "limit": 50,
    "remedy": "purchase_add_on"
  }
  ```
- **IDOR protection:** a scoped token can only prove it owns an operation's `calendarId`; the
  mutation additionally cross-checks that a resolved `windowId` actually belongs to that same
  `calendarId` before applying anything — pairing a calendar you own with a window that belongs
  to a different calendar fails the whole batch with the same not-found-shaped rejection used for
  a genuinely missing window.
- **RESTRICTED organizations:** rejected outright (HTTP 402-equivalent GraphQL error,
  `resource: "organization_restricted"`, `remedy: "resolve_billing"`), even for an update/delete-only
  batch.
- **On success:** `windows` returns **every** group-scoped window across the **entire slot's
  roster** (all calendars), not just the ones touched by this batch.

**Client migration notes:**
- **Web SPA (React):** if you build an admin UI for managing a group's roster availability, this
  is the batch endpoint to drive a "save all changes in this panel" action — treat a failed batch
  as fully unapplied and re-render from the returned `windows` list on success.
- **Partner integrations:** the idempotent-create behavior means you can safely retry a
  timed-out batch call without deduplication logic on your end. Do budget for the
  `availability_windows` limit being shared with base availability windows AND (per the metering
  change above) all blocked time.

---

## 2. Group-scoped blocked times

### REST — `.../calendar-groups/{group_id}/slots/{slot_id}/blocked-times/`

Direct mirror of the windows REST surface above (same auth model, same 404 non-disclosure, same
tri-state `rrule_string`, `PUT` unsupported), plus a `reason` field.

**`GroupScopedBlockedTime` (read shape):** same fields as `GroupScopedAvailabilityWindow` plus
`reason: string`.

**Create** (`POST`) — request body:

```json
{
  "calendar": 42,
  "start_time": "2026-09-10T12:00:00Z",
  "end_time": "2026-09-10T13:00:00Z",
  "timezone": "America/Sao_Paulo",
  "reason": "Lunch",
  "rrule_string": null
}
```

- `calendar`, `start_time`, `end_time`, `timezone` — same rules as windows.
- `reason` (string, optional, default `""`).
- `rrule_string` (string, optional).

Response: **201** with a `GroupScopedBlockWriteResult`:

```json
{
  "block": { "id": 601, "calendar_id": 42, "group_slot_id": 7, "start_time": "...", "end_time": "...", "timezone": "America/Sao_Paulo", "reason": "Lunch", "rrule_string": null, "is_recurring": false, "created": "...", "modified": "..." },
  "orphaned_bookings": []
}
```

Unlike windows, **every** block create and update runs orphan detection (not only the first
block) — each block independently subtracts time from the calendar, so any of them can orphan a
booking. `orphaned_bookings` uses the same minimal shape (`id`, `calendar_id`, `title`,
`start_time`, `end_time`).

**Update** (`PATCH /{id}/`) — same tri-state `rrule_string` semantics as windows; `reason` is also
independently optional (omit to leave unchanged).

**Delete** (`DELETE /{id}/`) — **204**, no body.

**Errors:** identical 404 non-disclosure shape as windows; **400** for `start_time >= end_time`.
Blocks are **not metered by themselves** in the sense of a dedicated per-block-write limit check
on the single-write REST path, but a `RESTRICTED` billing root still blocks the write (**402**).

### Public API (GraphQL) — `groupScopedBlockedTimes` query

Same shape and args as `groupScopedAvailabilityWindows`, requires the
`GROUP_SCOPED_BLOCKED_TIMES` resource grant. Returns
`[GroupScopedBlockedTimeGraphQLType]` (adds `reason: string`).

### Public API (GraphQL) — `batchUpsertGroupScopedBlockedTimes` mutation

Direct mirror of `batchUpsertGroupScopedAvailabilityWindows` — same validation, owner-scope, and
IDOR cross-check structure. `GroupScopedBlockedTimeOperationInput` adds `blockId` (instead of
`windowId`) and `reason` (optional on create/update). Idempotent-create content-match additionally
includes `reason` (an identical block including the same reason is a no-op; a different reason on
otherwise-identical times is a genuine new block).

**Key difference from windows:** blocked time is **not currently metered at the batch level
either** — this mutation never raises the plan-limit `OverLimitError` for a ceiling (only the
`RESTRICTED`-organization rejection can surface as an `OverLimitError`). Requires the
`BATCH_UPSERT_GROUP_SCOPED_BLOCKED_TIMES` resource grant.

> Reminder: even though this batch write isn't limit-gated per se, every blocked-time row it
> creates now **counts toward the `availability_windows` usage total** read elsewhere (dashboard,
> entitlement queries) — see the metering breaking-change note above.

**Client migration notes:**
- **Web SPA (React):** blocks and windows share the same batch UX pattern; a roster-management
  panel that already drives the windows batch can reuse the same request/response handling for
  blocks with the field renames above.
- **Partner integrations:** if you track "remaining availability_windows quota" client-side to
  pre-empt a 402, include blocked-time writes (base and group-scoped) in that local count now.

---

## 3. Group-scoped quota rules

Quota rules are the simplest of the three: non-recurring, no time range, and (as of this branch)
**unmetered** — creating one never counts against a plan limit. They also never orphan a booking:
a quota rule caps *future* bookings and can never invalidate one already confirmed, so there is no
`orphaned_bookings` field anywhere in this surface.

### REST — `.../calendar-groups/{group_id}/slots/{slot_id}/quota-rules/`

Same auth/visibility model as windows/blocks. `PUT` unsupported.

**`GroupScopedQuotaRule` (read shape):**

| Field | Type | Notes |
|---|---|---|
| `id` | int | |
| `calendar_id` | int | |
| `group_slot_id` | int | |
| `period` | `"day" \| "week" \| "month"` | fixed calendar period the cap applies to |
| `cap` | int | maximum live bookings made **through this group** per period; ≥ 1 |
| `created` | datetime | |
| `modified` | datetime | |

**Create** (`POST`):

```json
{ "calendar": 42, "period": "week", "cap": 3 }
```

- `calendar` (int, required), `period` (`"day"|"week"|"month"`, required), `cap` (int, required,
  ≥ 1).

Response: **201** with the `GroupScopedQuotaRule` object directly (**not** wrapped in a
write-result — there's no `orphaned_bookings` to carry).

**Update** (`PATCH /{id}/`) — `period` and/or `cap` optional, only provided fields change.
Response: **200** with the updated `GroupScopedQuotaRule`.

**Delete** (`DELETE /{id}/`) — **204**.

**Errors:**
- **404** — same non-disclosure shape as windows/blocks.
- **400** — the model enforces **one rule per (calendar, slot, period)**. Creating or updating a
  rule into a period that already has one for that calendar/slot is rejected as
  ```json
  { "non_field_errors": ["<constraint violation message>"] }
  ```
  never an unhandled 500. (A calendar CAN have both a daily and a weekly rule simultaneously —
  the uniqueness is per period, not per calendar+slot.)

### Public API (GraphQL) — `groupScopedQuotaRules` query

Same shape/args pattern as the other two list queries; requires the `GROUP_SCOPED_QUOTA_RULES`
resource grant. Returns `[GroupScopedQuotaRuleGraphQLType]` (`period` is a plain string —
`"day" | "week" | "month"`).

### Public API (GraphQL) — `batchUpsertGroupScopedQuotaRules` mutation

Mirrors the other two batch mutations' validation/owner-scope/IDOR structure.
`GroupScopedQuotaRuleOperationInput`: `action`, `calendarId` (required on every op), `ruleId`
(required for update/delete), `period`/`cap` (required for create, optional for update).

- **Not metered** — never raises `OverLimitError` for a plan ceiling; only the
  `RESTRICTED`-organization rejection (`resolve_billing` remedy) can surface via the same
  `OverLimitError` GraphQL error shape.
- **Idempotent create:** an identical create (same calendar, slot, period, cap) is a no-op.
- **Uniqueness conflict:** a create naming an already-used `(calendar, slot, period)` with a
  **different** cap returns `{ success: false, errorMessage: "<...>" }` — a clean failure result,
  never an unhandled error.
- Requires the `BATCH_UPSERT_GROUP_SCOPED_QUOTA_RULES` resource grant.

**Client migration notes:**
- **Web SPA (React):** quota rules are the only one of the three concepts with no orphan-warning
  UI to build — a create/update always succeeds cleanly or fails with a plain validation message.
- **Partner integrations:** no plan-limit pre-flight needed for quota rule writes; only the
  uniqueness constraint and org-restricted state can reject a write.

---

## Other contract changes

- **New public API resources** (add to any token's `OrganizationResourceAccess` grant list as
  needed): `group_scoped_availability_windows`, `batch_upsert_group_scoped_availability_windows`,
  `group_scoped_blocked_times`, `batch_upsert_group_scoped_blocked_times`,
  `group_scoped_quota_rules`, `batch_upsert_group_scoped_quota_rules`. All six are provider-scoped
  resources (subject to the same owner-scope filtering as the rest of the calendar/availability
  surface).
- **New `GroupScopedRuleType` values** used in booking-rejection messages: `outside_window`,
  `inside_block`, `quota_consumed`. Not a field anywhere in a success response — only ever
  embedded in a rejection's error text (see the rejection-shape section above).
- No changes to rate limits, webhooks, file-upload conventions, or the public API's auth header
  format (`Authorization: Bearer <systemUserId>:<token>`).

## Rollout

Everything in this document is live in the branch's code — no feature flag. It is self-gating in
the product sense: a group slot with no windows/blocks/quota rules configured behaves exactly as
before, so nothing changes for an existing integration until an admin deliberately creates one of
these three things. The one exception is the metering behavior change (blocked time now counts
toward `availability_windows`), which takes effect immediately once this branch is deployed,
regardless of whether anyone uses the new group-scoped features — see **Breaking changes** above.

No cross-repo deploy ordering constraint. Client teams can build against this document once the
branch's phases are merged to `main`; there is nothing further this repo needs to do to make these
operations available.
