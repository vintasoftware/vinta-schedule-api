# Single-Use Scheduling Codes (Public GraphQL API) — Implementation Plan

Spec sibling: [2026-06-17-SINGLE_USE_SCHEDULING_CODES_SPEC.md](2026-06-17-SINGLE_USE_SCHEDULING_CODES_SPEC.md). This plan translates that
spec into phases; it does not re-derive requirements. Read the spec's **Decisions → Use-cases**
and **Decisions → Acceptance scenarios** alongside this plan.

## 1. Goals

1. Expose **org-token-gated mint mutations** that create single-use codes scoped to a calendar
   or calendar-group (booking) or to a specific event (reschedule / cancel), backed by the
   existing `CalendarManagementToken`.
2. Expose an **org-token-gated revoke mutation** that invalidates an unused code by its opaque id.
3. Expose **unauthenticated, code-bearing patient mutations** that book, reschedule, and cancel
   events authorized solely by a code (no `IsAuthenticated` / `OrganizationResourceAccess`).
4. Expose **unauthenticated, code-gated availability read fields** so a patient can choose a slot
   for a code's bound calendar/group without an org token, without consuming the code.
5. Enforce code lifecycle exactly: reusable for reads, **single-use on first successful write**,
   atomic consumption, optional expiry, revocable, org- and scope-bound.

**Non-goals:**
- Patient portal UI (API only).
- The `is_private` / restricted-visibility flag on `Calendar` / `CalendarGroup` / bundles —
  separate, independently-planned change. This plan assumes it will exist.
- Per-user / patient-scoped Public API tokens — separate spec.
- The `user_created` outgoing webhook — separate spec.
- An `AppointmentType` entity / `appointmentTypeId` — no such model; codes bind to the
  calendar/group directly.
- Bundle-specific mint mutations — bundles (`calendar_type=BUNDLE`) ride the calendar-shaped
  mutations.
- A feature-flag system — none exists in the repo and this surface is purely additive (see
  **Guiding Decisions**).
- Changing the existing authenticated provider mutations (`createCalendarEvent`,
  `createCalendarGroupEvent`, `rescheduleCalendarEvent`, etc.) or existing read resolvers.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **No feature flag** | Repo has no flag system ([confirmed: none — no waffle/django-flags]). Every phase ships a **brand-new additive GraphQL field** (new mutation or new query field); no existing resolver, schema field, or row-write path changes. Per the plan skill's "purely additive new surface" rule, no flag is warranted and building a flag module is out of scope. Risk controlled instead by per-field tests + the fact that off-path callers never reach new code. |
| **Reuse existing model** | `CalendarManagementToken` + `CalendarManagementTokenPermission` already model token hash, scope (calendar/event/external_attendee), permissions, `used_at`, `revoked_at`. We add only the columns the spec's lifecycle/audit needs, rather than a new table — keeps one consumption/validation path. |
| **Consumption is single-use-on-success, atomic** | `used_at` exists but is never set today. Add a manager `consume()` that sets `used_at` under a row lock inside the same transaction as the action, so a code cannot double-book (spec **Decisions → State transitions & edge cases**, concurrency). Reads never call it. |
| **Expiry stored, no default** | Add `expires_at` (nullable). No default TTL (spec decision). A non-null past `expires_at` ⇒ `EXPIRED`. |
| **Audit columns** | Add `minted_by_system_user` FK (who minted) and `consumed_source_ip` (who burned it). Satisfies spec **Open Questions** audit default without a new audit surface. |
| **Code-gated reads are new fields** | New dedicated query fields (`availableTimesWithCode`, `availabilityWindowsWithCode`, `unavailableWindowsWithCode`, `calendarGroupBookableSlotsWithCode`, `calendarGroupAvailabilityWithCode`) rather than an optional arg on existing resolvers — keeps existing authenticated read contracts byte-for-byte and avoids needing a flag. |
| **With-code mutations follow the `check_token` precedent** | The only existing unauthenticated field, `check_token` ([public_api/mutations.py:56-73](../public_api/mutations.py#L56-L73)), declares **no** `permission_classes` and resolves via DI services. Code-bearing fields do the same: no permission class (a Strawberry permission class can't read input args anyway); the resolver calls `CalendarPermissionService.initialize_with_token(...)` to validate, then acts. |
| **Org resolved from the code** | The code's `CalendarManagementToken` row carries `organization_id`. Resolver loads the token, derives the org, and runs the action within that org. The existing per-org rate limiter ([public_api/extensions.py]) keys on that org; no extra lockout (spec **Risks assumed**). |
| **New resource for minting** | Add `PublicAPIResources.CALENDAR_BOOKING_CODE`; org token must be granted it to call any mint/revoke mutation. Add the six mint fields + revoke to `FIELD_TO_RESOURCE_MAPPING`. With-code patient fields are **absent** from the mapping (unauthenticated). |
| **Revoke by opaque id** | Mint result returns `{ code, id }`; `revokeBookingCode(id)` sets `revoked_at`. Minter need not retain plaintext. |
| **Result shape** | `success: bool` + `errorMessage: str | None` + a machine-readable `errorCode` enum (`INVALID_CODE`, `EXPIRED`, `ALREADY_USED`, `REVOKED`, `NOT_PERMITTED`, `SLOT_UNAVAILABLE`) + optional typed payload (`code`+`id` for mint, `event` for writes). Matches existing `success`/`error_message` result types ([calendar_integration/mutations.py:344-361](../calendar_integration/mutations.py#L344-L361)). |
| **Binding** | Booking code → calendar OR group (bundle = calendar). Reschedule/cancel code → one event; mint rejects an event that does not belong to the named calendar/group. |

## 3. Data Model Changes

### 3.1 `CalendarManagementToken` new fields

In [calendar_integration/models.py:1633-1671](../calendar_integration/models.py#L1633-L1671):

```python
class CalendarManagementToken(OrganizationModel):
    # ... existing: calendar, event, token_hash, used_at, revoked_at, user, external_attendee
    expires_at = models.DateTimeField(null=True, blank=True)
    minted_by_system_user = models.ForeignKey(
        "public_api.SystemUser", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="minted_management_tokens",
    )
    consumed_source_ip = models.GenericIPAddressField(null=True, blank=True)
```

Standard Django migration (the model uses plain Django migrations, not the raw-SQL framework —
e.g. [calendar_integration/migrations/0006_calendareventupdatetoken_and_more.py]). All additions
nullable/additive — safe, reversible, no rewrite of existing rows.

### 3.2 `CalendarManagementToken` manager + lifecycle methods

Add a custom manager/queryset with:
- `active()` — `used_at__isnull=True, revoked_at__isnull=True` and (`expires_at__isnull=True` or
  `expires_at__gt=now`).
- `consume(token, source_ip)` — `select_for_update()` the row, re-check it is still active, set
  `used_at=now()` + `consumed_source_ip`, save. Returns/raises so the caller commits the action
  in the same transaction. This is the atomic first-write-wins gate.
- A `status`/validation helper returning the spec's `errorCode` (`EXPIRED` / `ALREADY_USED` /
  `REVOKED`) for a presented token, so resolvers map failures uniformly.

### 3.3 Type plumbing

- New `EventManagementPermissions` are already defined (create / reschedule / cancel etc.) —
  no enum change.
- Add a Strawberry enum `BookingCodeErrorCode` (mirrors the lifecycle/validation errors) used by
  every result type in this feature.
- Add shared result types: `BookingCodeResult { success, errorCode, errorMessage, code, id }`
  and `CodeEventResult { success, errorCode, errorMessage, event }`.

## 4. API Design

All fields are added in [calendar_integration/mutations.py](../calendar_integration/mutations.py) /
[calendar_integration/graphql.py](../calendar_integration/graphql.py) (queries) and reach the
Public API through the existing merge (`public_api.mutations.Mutation` extends
`CalendarGroupMutations`; queries via `public_api.queries.Query`).

### 4.1 Mint mutations (org-token-gated)

`permission_classes=[IsAuthenticated, OrganizationResourceAccess]`; mapped to
`CALENDAR_BOOKING_CODE`.

- `createCalendarBookingCode(input: { organizationId, calendarId, expiresAt })` → `BookingCodeResult`
- `createCalendarGroupBookingCode(input: { organizationId, calendarGroupId, expiresAt })` → `BookingCodeResult`
- `createCalendarRescheduleBookingCode(input: { organizationId, calendarId, eventId, expiresAt })` → `BookingCodeResult`
- `createCalendarGroupRescheduleBookingCode(input: { organizationId, calendarGroupId, eventId, expiresAt })` → `BookingCodeResult`
- `createCalendarCancellationBookingCode(input: { organizationId, calendarId, eventId, expiresAt })` → `BookingCodeResult`
- `createCalendarGroupCancellationBookingCode(input: { organizationId, calendarGroupId, eventId, expiresAt })` → `BookingCodeResult`
- `revokeBookingCode(input: { organizationId, id })` → `BookingCodeResult` (no `code` echoed back)

### 4.2 With-code mutations (unauthenticated)

No `permission_classes`. Authorized by `code` in the input.

- `createCalendarEventWithCode(input: { code, title, description, startTime, endTime, timezone, externalAttendee: { email, name } })` → `CodeEventResult`
- `createCalendarGroupEventWithCode(input: { code, title, description, startTime, endTime, timezone, slotSelections: [{ slotId, calendarIds }], externalAttendee: { email, name } })` → `CodeEventResult`
- `rescheduleCalendarEventWithCode(input: { code, startTime, endTime, timezone })` → `CodeEventResult`
- `rescheduleCalendarGroupEventWithCode(input: { code, startTime, endTime, timezone, slotSelections: [{ slotId, calendarIds }] })` → `CodeEventResult`
- `cancelEventWithCode(input: { code })` → `CodeEventResult`

### 4.3 Code-gated read fields (unauthenticated)

No `permission_classes`; `code` argument gates scope; never consumes.

- `availableTimesWithCode(code, startDatetime, endDatetime)`
- `availabilityWindowsWithCode(code, startDatetime, endDatetime)`
- `unavailableWindowsWithCode(code, startDatetime, endDatetime)`
- `calendarGroupBookableSlotsWithCode(code, searchWindowStart, searchWindowEnd, durationSeconds)`
- `calendarGroupAvailabilityWithCode(code, startDatetime, endDatetime)`

Each resolves the code → bound calendar/group/event, returns availability **only** for that
bound scope, and errors (`INVALID_CODE` / `EXPIRED` / `REVOKED` / `NOT_PERMITTED`) otherwise.

## 5. Phased Rollout

No feature flag (see **Guiding Decisions**), therefore no flag-removal phase. Phases are ordered
foundation → mint → revoke → reads → writes, so that by the time the unauthenticated write
phases land, codes can be minted, revoked, and read against. Each phase is independently
mergeable and reversible (drop the field + revert the migration).

### Phase 0 — Token lifecycle foundation

**Goal**: ship the model columns, atomic consume path, standalone-code service methods, and the
new permission resource that every later phase consumes. No user-visible behavior on its own.

**Feature flag**: none — scaffolding, no reachable GraphQL field yet.

Changes:
1. [calendar_integration/models.py](../calendar_integration/models.py): add `expires_at`,
   `minted_by_system_user`, `consumed_source_ip`; add manager `active()` / `consume()` /
   validation-status helper (**Data Model Changes**).
2. New Django migration for the three columns.
3. [calendar_integration/services/calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py):
   add `create_booking_token(organization_id, calendar_id=None, calendar_group_id=None, event_id=None, permissions, expires_at, minted_by)` returning `(token, plaintext_code)`; add
   `validate_code(code) -> token` and `consume_code(token, source_ip)` wrappers over the manager;
   reuse existing `initialize_with_token` / hashing helpers.
4. [public_api/constants.py](../public_api/constants.py): add `CALENDAR_BOOKING_CODE`.
5. [public_api/permissions.py](../public_api/permissions.py): add the six mint fields + `revokeBookingCode` to `FIELD_TO_RESOURCE_MAPPING` → `CALENDAR_BOOKING_CODE`.
6. Add `BookingCodeErrorCode` enum + `BookingCodeResult` / `CodeEventResult` result types
   (shared module under [calendar_integration/graphql.py](../calendar_integration/graphql.py)).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `calendar_integration/tests/test_management_token_manager.py` — `active()` filtering
  (used / revoked / expired / live); `consume()` sets `used_at`+IP once and is a no-op-then-error
  on a second call; concurrency test that two `consume()` under `select_for_update` yield exactly
  one success.
- **Unit**: `calendar_integration/tests/test_calendar_permission_service_codes.py` — `create_booking_token` persists scope+permissions+expiry+minter; `validate_code` returns the right `errorCode` for each terminal state.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches model
+ migration + service + concurrency-sensitive consume across several files.

**Reusable skills**: `add-migration` (the three-column migration); `add-model` (manager/queryset
pattern); `write-tests` (manager + service tests).

Acceptance: migration applies and reverts cleanly; `consume()` is atomic single-use under
concurrent access; `CALENDAR_BOOKING_CODE` exists and the mint/revoke field names map to it.

### Phase 1 — Mint booking codes

**Goal**: a provider/admin with an org token mints a single-use **booking** code for a calendar
or group.

**Feature flag**: none — new additive mutations.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `createCalendarBookingCode` and `createCalendarGroupBookingCode`, both
   `permission_classes=[IsAuthenticated, OrganizationResourceAccess]`, delegating to
   `CalendarPermissionService.create_booking_token(..., permissions=[CREATE])`, setting
   `minted_by` from `request.public_api_system_user`. Return `BookingCodeResult { code, id }`.

Spec use-case: **Decisions → Use-cases**, Use-case 1 (provider mints a booking code).

Tests:
- **Integration**: `public_api/tests/test_booking_code_mutations.py` — org token with
  `CALENDAR_BOOKING_CODE` mints calendar + group codes (asserts token row scope/permission/minter);
  token **without** the resource is rejected; a bundle calendar (`calendar_type=BUNDLE`) mints via
  the calendar mutation.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Thin
mutation over the Phase 0 service, exact precedent in existing calendar mutations.

**Reusable skills**: `create-graphql-public-query` (field + mapping wiring); `write-tests`.

Acceptance: both mint mutations return a usable code + id and create a correctly-scoped
create-permission token; unauthorized tokens are rejected.

### Phase 2 — Mint reschedule & cancel codes

**Goal**: a provider/admin mints a single-use **reschedule** or **cancel** code bound to one
specific event.

**Feature flag**: none — new additive mutations.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `createCalendarRescheduleBookingCode`, `createCalendarGroupRescheduleBookingCode`,
   `createCalendarCancellationBookingCode`, `createCalendarGroupCancellationBookingCode` — same
   auth, permissions `[RESCHEDULE]` / `[CANCEL]`, event-scoped. Reject mint when `eventId` does
   not belong to the named calendar/group.

Spec use-case: **Decisions → Use-cases**, Use-case 3 (provider mints a reschedule/cancel code).

Tests:
- **Integration**: `public_api/tests/test_reschedule_cancel_code_mutations.py` — each of the four
  mutations creates an event-scoped token with the right single permission; mismatched
  event↔calendar/group is rejected; missing-resource token rejected.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Four
parallel mutations following Phase 1's pattern.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: the four event-bound mint mutations produce correctly-scoped single-permission
tokens and reject event/scope mismatches.

### Phase 3 — Revoke codes

**Goal**: a provider/admin invalidates an unused code by its opaque id.

**Feature flag**: none — new additive mutation.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `revokeBookingCode(input: { organizationId, id })`, org-token-gated, sets `revoked_at` on the
   org-scoped token. Returns `BookingCodeResult` (no `code`). Idempotent on an already-revoked
   code; `INVALID_CODE` for unknown/cross-org id.

Spec use-case: **Decisions → Use-cases**, Use-case 5 (provider revokes an unused code).

Tests:
- **Integration**: `public_api/tests/test_revoke_booking_code.py` — mint → revoke → row has
  `revoked_at`; revoke of another org's token id returns `INVALID_CODE`; revoke is idempotent.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`.
Single small mutation.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: `revokeBookingCode` revokes own-org codes by id, rejects cross-org ids, is idempotent.

### Phase 4 — Code-gated availability reads

**Goal**: an unauthenticated patient reads availability for a code's bound calendar/group to pick
a slot, without consuming the code.

**Feature flag**: none — new additive query fields.

Changes:
1. [calendar_integration/graphql.py](../calendar_integration/graphql.py) (+ registration on
   [public_api/queries.py](../public_api/queries.py)): add `availableTimesWithCode`,
   `availabilityWindowsWithCode`, `unavailableWindowsWithCode`,
   `calendarGroupBookableSlotsWithCode`, `calendarGroupAvailabilityWithCode` — **no**
   `permission_classes`. Each validates the code (active/permission/scope), resolves the bound
   calendar/group, and returns availability for that scope only. Never calls `consume()`.

Spec use-case: supports **Decisions → Use-cases**, Use-cases 2 and 4 (the availability-read steps).

Tests:
- **Integration**: `public_api/tests/test_code_gated_reads.py` — a booking code returns
  availability for its bound calendar/group with no org token; reading a *different* calendar
  fails; reads do **not** set `used_at`; expired/revoked codes return the right `errorCode`.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Five new
unauthenticated resolvers reusing availability services with scope-binding logic.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: each code-gated read returns only its bound scope's availability, requires no org
token, and never consumes the code.

### Phase 5a — Book single-calendar event with code

**Goal**: an unauthenticated patient creates an event on the bound calendar using a booking code.

**Feature flag**: none — new additive unauthenticated mutation.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `createCalendarEventWithCode` (no `permission_classes`). Validate code (active, `CREATE`,
   calendar scope), build `CalendarEventInputData` with the input's `externalAttendee`, call
   `CalendarService.create_event` and `consume_code(token, request_ip)` in **one transaction**.
   Map failures to `errorCode` (`SLOT_UNAVAILABLE` leaves the code live).

Spec use-case: **Decisions → Use-cases**, Use-case 2 (patient books — single-calendar variant).

Tests:
- **Integration**: `public_api/tests/test_book_with_code.py` — happy path creates the event +
  consumes the code with **no** org token; replay → `ALREADY_USED`; slot-taken failure leaves the
  code `Active`; wrong-permission/cross-scope/cross-org codes rejected; expired/revoked rejected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Unauthenticated
write with transactional consume + edge-case branching.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: a valid booking code creates exactly one event and is consumed atomically; failed
writes do not consume; no org token required.

### Phase 5b — Book calendar-group event with code

**Goal**: same as 5a for a **group** booking code (with slot selections).

**Feature flag**: none — new additive unauthenticated mutation.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `createCalendarGroupEventWithCode` — validate group-scoped `CREATE` code, build group event
   input with `slotSelections` + `externalAttendee`, call the group event create service
   ([calendar_group_service.py:384](../calendar_integration/services/calendar_group_service.py#L384))
   + `consume_code` in one transaction.

Spec use-case: **Decisions → Use-cases**, Use-case 2 (patient books — group variant).

Tests:
- **Integration**: `public_api/tests/test_book_group_with_code.py` — happy path + consume; replay
  `ALREADY_USED`; invalid slot selection leaves code live; cross-scope/org rejected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Group create
path with slot selections + transactional consume.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: a valid group booking code creates one grouped event and is consumed atomically.

### Phase 6a — Reschedule single-calendar event with code

**Goal**: an unauthenticated patient reschedules the bound event using a reschedule code.

**Feature flag**: none — new additive unauthenticated mutation.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `rescheduleCalendarEventWithCode` — validate event-scoped `RESCHEDULE` code, call
   `CalendarService.update_event` with new times + `consume_code` in one transaction.

Spec use-case: **Decisions → Use-cases**, Use-case 4 (patient reschedules — single-calendar
variant).

Tests:
- **Integration**: `public_api/tests/test_reschedule_with_code.py` — happy path moves the event +
  consumes; replay `ALREADY_USED`; code bound to event E cannot touch event F (`INVALID_CODE`);
  a `CANCEL`-permission code is rejected (`NOT_PERMITTED`); expired/revoked rejected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Transactional
write + scope/permission negatives.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: a valid reschedule code moves exactly its bound event once and is consumed; wrong
permission/event/org rejected.

### Phase 6b — Reschedule calendar-group event with code

**Goal**: same as 6a for a group reschedule code (with slot selections).

**Feature flag**: none — new additive unauthenticated mutation.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `rescheduleCalendarGroupEventWithCode` — validate group/event-scoped `RESCHEDULE` code, apply
   new times + `slotSelections` via the group reschedule path + `consume_code` in one transaction.

Spec use-case: **Decisions → Use-cases**, Use-case 4 (patient reschedules — group variant).

Tests:
- **Integration**: `public_api/tests/test_reschedule_group_with_code.py` — happy path + consume;
  replay `ALREADY_USED`; scope/permission/org negatives.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: a valid group reschedule code reschedules its bound grouped event once and is
consumed.

### Phase 6c — Cancel event with code

**Goal**: an unauthenticated patient cancels the bound event using a cancel code.

**Feature flag**: none — new additive unauthenticated mutation.

Changes:
1. [calendar_integration/mutations.py](../calendar_integration/mutations.py): add
   `cancelEventWithCode(input: { code })` — validate event-scoped `CANCEL` code, call
   `CalendarService.delete_event` + `consume_code` in one transaction. Returns `CodeEventResult`
   (cancelled event id where available).

Spec use-case: **Decisions → Use-cases**, Use-case 4 (patient cancels).

Tests:
- **Integration**: `public_api/tests/test_cancel_with_code.py` — happy path cancels + consumes;
  replay `ALREADY_USED`; a `RESCHEDULE`-permission code rejected (`NOT_PERMITTED`); cross-event/org
  rejected; expired/revoked rejected.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Smallest
of the write mutations (no time/slot inputs), following 6a's transactional pattern.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: a valid cancel code cancels exactly its bound event once and is consumed; wrong
permission/event/org rejected.

## 6. Risk & Rollout Notes

- **Feature flag**: none. Justified in **Guiding Decisions** — every phase is a brand-new
  additive GraphQL field; no existing resolver, schema field, or write path changes, so off-path
  callers are byte-for-byte unaffected. Rollback for any phase = remove the field (+ revert the
  Phase 0 migration if rolling back the foundation).
- **Migration safety (Phase 0)**: three nullable column adds on `CalendarManagementToken` — no
  rewrite, no lock of concern on a low-volume table. Reverse migration drops the columns. No
  backfill needed (`used_at`/`expires_at` semantics apply only to new codes).
- **Concurrency**: the single-use guarantee rests on `consume()` using `select_for_update()` and
  committing in the same transaction as the action. Phase 0 ships the dedicated concurrency test;
  every write phase asserts replay → `ALREADY_USED`.
- **Unauthenticated surface**: code-bearing fields carry no `permission_classes` by design. Risk
  controlled by opaque high-entropy hashed codes (existing scheme), constant-time comparison, and
  returning the **same** error category for "wrong target" as "unknown code" so existence isn't
  leaked. Reuses the existing per-org rate limiter (org resolved from the code).
- **Rollback story**: phases are independently reversible. Removing a with-code mutation cannot
  strip already-created events; it only stops new code-authorized writes. Removing the foundation
  columns is safe once the dependent field phases are removed.
- **Deploy ordering**: all in-repo; no cross-repo producer blocks deploy. Phase 0 must deploy
  before any field phase (provides columns + service + resource). The Medplum bot (external
  consumer) integrates after the mint + with-code phases ship, but does not gate our deploys.

## 7. Open Questions

Carried from the spec (each has a recommended default so no phase is blocked):

1. **Exact code-gated read set** (Phase 4). Default: the five fields listed in **API Design**.
   Owner: integration team (which portal screens read what). Unblocks: Phase 4 field list.
2. **Whether code-gated reads should additionally require the target be `is_private`** or work
   for any bound target. Default: the binding is the gate, independent of privacy. Owner: security
   owner once `is_private` lands. Unblocks: Phase 4 authorization tests.
3. **Group reschedule path** — confirm the exact group-service method used to reschedule a grouped
   event with new slot selections (Phase 6b). Default: the group service's update/reschedule path
   analogous to `create_grouped_event`. Owner: calendar-integration maintainer. Unblocks: Phase 6b
   implementation. (Flag in-phase if the method signature differs from the spec's assumption.)
4. **Audit depth** — whether `minted_by_system_user` + `consumed_source_ip` suffice or a separate
   audit-log entry is wanted per consume. Default: columns only. Owner: compliance. Unblocks: any
   tightening of Phase 0 model.

## 8. Touch List

**Phase 0**
- edit [calendar_integration/models.py](../calendar_integration/models.py) — token columns + manager.
- new `@calendar_integration/migrations/00XX_booking_code_columns.py`.
- edit [calendar_integration/services/calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py) — `create_booking_token` / `validate_code` / `consume_code`.
- edit [public_api/constants.py](../public_api/constants.py) — `CALENDAR_BOOKING_CODE`.
- edit [public_api/permissions.py](../public_api/permissions.py) — mint/revoke → resource mapping.
- edit [calendar_integration/graphql.py](../calendar_integration/graphql.py) — `BookingCodeErrorCode`, `BookingCodeResult`, `CodeEventResult`.
- new `@calendar_integration/tests/test_management_token_manager.py`, `@calendar_integration/tests/test_calendar_permission_service_codes.py`.

**Phase 1**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `createCalendarBookingCode`, `createCalendarGroupBookingCode`.
- new `@public_api/tests/test_booking_code_mutations.py`.

**Phase 2**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — four reschedule/cancel mint mutations.
- new `@public_api/tests/test_reschedule_cancel_code_mutations.py`.

**Phase 3**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `revokeBookingCode`.
- new `@public_api/tests/test_revoke_booking_code.py`.

**Phase 4**
- edit [calendar_integration/graphql.py](../calendar_integration/graphql.py) + [public_api/queries.py](../public_api/queries.py) — five code-gated read fields.
- new `@public_api/tests/test_code_gated_reads.py`.

**Phase 5a**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `createCalendarEventWithCode`.
- new `@public_api/tests/test_book_with_code.py`.

**Phase 5b**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `createCalendarGroupEventWithCode`.
- new `@public_api/tests/test_book_group_with_code.py`.

**Phase 6a**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `rescheduleCalendarEventWithCode`.
- new `@public_api/tests/test_reschedule_with_code.py`.

**Phase 6b**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `rescheduleCalendarGroupEventWithCode`.
- new `@public_api/tests/test_reschedule_group_with_code.py`.

**Phase 6c**
- edit [calendar_integration/mutations.py](../calendar_integration/mutations.py) — `cancelEventWithCode`.
- new `@public_api/tests/test_cancel_with_code.py`.
