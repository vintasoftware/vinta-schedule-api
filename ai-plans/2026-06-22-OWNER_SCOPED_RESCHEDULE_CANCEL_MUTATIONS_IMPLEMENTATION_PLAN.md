# Owner-Scoped Public Reschedule / Cancel Mutations — Implementation Plan

> No `..._SPEC.md` sibling exists for this feature; the source of truth is the originating
> task brief plus the decisions captured in **Guiding Decisions** below (resolved via the
> planning interview). The closest precedents are
> [2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_IMPLEMENTATION_PLAN.md](./2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_IMPLEMENTATION_PLAN.md)
> (the owner-scoped `scheduleEvent` write path) and
> [2026-06-17-SINGLE_USE_SCHEDULING_CODES_IMPLEMENTATION_PLAN.md](./2026-06-17-SINGLE_USE_SCHEDULING_CODES_IMPLEMENTATION_PLAN.md)
> (the `*WithCode` reschedule/cancel family, which is explicitly **out of scope** here).

## 1. Goals

1. Add three authenticated Public GraphQL mutations on the `Mutation` class in
   [public_api/mutations.py](../public_api/mutations.py): `rescheduleCalendarEvent`,
   `rescheduleCalendarGroupEvent`, and `cancelEvent`, wrapping
   `CalendarEventService.update_event` / `CalendarEventService.delete_event` (and the group
   facade `CalendarGroupService.reschedule_grouped_event` / `cancel_grouped_event`).
2. Enforce the owner-scope contract via `assert_calendar_in_owner_scope` exactly as
   `scheduleEvent` does: an **owner-scoped** token may only reschedule/cancel events on
   calendars its owner owns; an **org-wide** token acts org-wide. Cross-owner and missing
   targets return the identical `"Calendar not found."` error — no existence leak.
3. Extend the service layer so public `SystemUser` tokens (owner-scoped **and** org-wide)
   may update/delete events — today both methods reject *every* `SystemUser`. The new
   allowance is the minimal, independently-verified ownership check; `create_event` stays
   org-wide-blocked (unchanged).
4. Support recurring **series vs. single-occurrence** semantics: whole-event/series ops via
   `update_event`/`delete_event`, and single-occurrence ops addressed by `recurrenceId` via
   the exception primitives (`create_exception` / modified-occurrence exception).

**Non-goals:**

- Do **not** touch the existing `rescheduleCalendarEventWithCode`,
  `rescheduleCalendarGroupEventWithCode`, or `cancelEventWithCode` mutations or their
  `CodeEventResult` flow — they already ship and are unauthenticated booking-code paths.
- No new database tables, columns, or migrations. `EventRecurrenceException`,
  `RecurrenceRule`, and the recurrence fields on `CalendarEvent` already exist.
- No org-wide **create** allowance — `create_event` remains owner-scoped-only.
- No feature flag — purely additive public surface (see **Guiding Decisions**).
- No per-occurrence reschedule/cancel for **group** events in v1 (group events are treated
  as whole-event only); only single-calendar events support `recurrenceId` ops.
- No re-checking of non-primary calendar availability on group reschedule (mirrors the
  existing `reschedule_grouped_event` v1 limitation).
- No REST surface; GraphQL only.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Authorization model** | Org-wide public tokens reschedule/cancel **any** event in the org; owner-scoped tokens are limited to calendars their owner owns. This is *why* we diverge from `create_event` (which blocks org-wide): the brief calls for "org-wide tokens act org-wide" on reschedule/cancel. The GraphQL layer enforces it with `assert_calendar_in_owner_scope` (no-op for org-wide, restricts scoped); the service layer re-verifies independently as defense-in-depth. |
| **Service-layer write allowance** | Today `update_event`/`delete_event` raise `PermissionDenied("Events cannot be created through the Public API.")` for *all* `SystemUser` principals ([calendar_event_service.py:649](../calendar_integration/services/calendar_event_service.py#L649), [:1471](../calendar_integration/services/calendar_event_service.py#L1471)). We replace the blanket rejection with a shared seam `_public_token_may_write(system_user, calendar)`: **org-wide → allowed**; **owner-scoped → allowed iff** `_scoped_system_user_owns_calendar` (the existing create-side helper) confirms ownership; else `PermissionDenied`. The `*WithCode` family is unaffected because it authenticates with a `CalendarManagementToken` (a `str`), not a `SystemUser`. |
| **Result shape** | Mirror `scheduleEvent`: reschedule mutations return `CalendarEventGraphQLType` directly and raise `GraphQLError` on any failure (including `"Calendar not found."` for cross-owner / missing). `cancelEvent` returns a new minimal `CancelEventResult { success: bool }` (delete returns nothing) and raises `GraphQLError` on failure. *Why not* `CodeEventResult`: that wrapper carries `BookingCodeErrorCode`, which is meaningless for token-authenticated calls; the brief says "mirror `scheduleEvent`'s" shape. |
| **Series vs. single-occurrence** | Inputs carry an optional `recurrenceId` (a `DateTime` = the occurrence's original start, matching `CalendarEvent.recurrence_id`). **Absent** → whole event/series. **Present** → single occurrence: cancel via `master.create_exception(recurrence_id, is_cancelled=True)`; reschedule via a modified-occurrence exception (`create_event(parent_event_id=master, is_recurring_exception=True, …)` linked through `create_exception(..., is_cancelled=False, modified_object=…)`). *Why a new service method*: un-materialized occurrences have no `event_id`, and passing a recurring **master** id to `delete_event` with `delete_series=False` would delete the master outright — a footgun we must not expose. |
| **Recurrence rule on reschedule** | Whole-series reschedule accepts an optional `rruleString`. If provided, the rule is updated; **if omitted, the existing rule is preserved by re-passing the master's current rrule string** into `update_event`. *Why*: `update_event` deletes the rule when `recurrence_rule` is `None` ([calendar_event_service.py:763-766](../calendar_integration/services/calendar_event_service.py#L763-L766)) — a latent strip-the-series bug we must avoid. `deleteSeries=true` reschedule is not a thing; series-conversion is out of scope. |
| **Cancel grouping** | A single `cancelEvent` mutation handles both single-calendar and grouped events by branching on `event.calendar_group_fk_id`, exactly like `cancelEventWithCode`. Grouped → `CalendarGroupService.cancel_grouped_event` (also deletes the linked non-primary `BlockedTime` rows); single → `delete_event`. |
| **Resource mapping** | All three fields map to `PublicAPIResources.CALENDAR_EVENT` in `FIELD_TO_RESOURCE_MAPPING` (the brief says "reuse `CALENDAR_EVENT`"). `CALENDAR_EVENT` is **already** in `PROVIDER_SCOPED_RESOURCES` ([public_api/constants.py:64-80](../public_api/constants.py#L64-L80)), so provider/owner-scoped tokens can already be granted it — **no `PROVIDER_SCOPED_RESOURCES` change is required**. |
| **Feature flag** | **No flag — purely additive surface.** The three fields are brand-new; the new service branch fires only for `SystemUser` principals that currently always error, so no existing *successful* caller changes behavior. A regression test asserts `create_event` for an org-wide token stays blocked. |
| **Bundle events** | **Amended 2026-06-23.** Bundle events **ARE** reschedulable/cancellable through the Public API. Updating/deleting an *existing* bundle **primary** event is a well-defined operation (`_update_bundle_event` / `_delete_bundle_event` fan the change out to children) — unlike `create_event`, where create on a bundle calendar fans out into problematic per-child *creates* (so create stays bundle-blocked). The only gate for reschedule/cancel is `_public_token_may_write` (owner-scoped tokens must own the bundle calendar; org-wide acts org-wide). Non-primary bundle *child* events remain rejected by the existing `is_bundle_event` `ValueError`. Do **not** re-introduce a bundle-calendar `PermissionDenied` in `update_event` / `delete_event` / `_load_recurring_master_for_occurrence`. |
| **Migrations** | None. No schema change. |

## 3. Data Model Changes

None. This feature is pure service + GraphQL wiring over existing models
(`CalendarEvent`, `EventRecurrenceException`, `RecurrenceRule`, `SystemUser`,
`CalendarOwnership`).

### 3.1 Type plumbing (new GraphQL input/result types — Phase 1–3)

New Strawberry types in [public_api/mutations.py](../public_api/mutations.py), mirroring
`ScheduleEventInput` / `ScheduleEventExternalAttendeeInput`:

```python
@strawberry.input
class RescheduleCalendarEventInput:
    organization_id: int
    calendar_id: int
    event_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str
    # Optional: change the series' recurrence pattern. Omit to PRESERVE the existing rule.
    rrule_string: str | None = None
    # Optional: when set, reschedule ONLY this occurrence of a recurring series
    # (the occurrence's original start, == CalendarEvent.recurrence_id). Omit for whole event/series.
    recurrence_id: datetime.datetime | None = None

@strawberry.input
class RescheduleCalendarGroupEventInput:
    organization_id: int
    event_id: int
    start_time: datetime.datetime
    end_time: datetime.datetime
    timezone: str

@strawberry.input
class CancelEventInput:
    organization_id: int
    calendar_id: int
    event_id: int
    delete_series: bool = False
    recurrence_id: datetime.datetime | None = None  # cancel one occurrence when set

@strawberry.type
class CancelEventResult:
    success: bool
```

> `rescheduleCalendarEvent` / `rescheduleCalendarGroupEvent` return `CalendarEventGraphQLType`
> directly; `cancelEvent` returns `CancelEventResult`.

## 4. API Design

All three are registered on the existing `Mutation` class in
[public_api/mutations.py](../public_api/mutations.py), decorated
`@strawberry.mutation(permission_classes=[IsAuthenticated, OrganizationResourceAccess])`.

### 4.1 `rescheduleCalendarEvent(input: RescheduleCalendarEventInput!): CalendarEventGraphQLType!`

- Resolve org + init calendar service (`_get_org_and_init_calendar_service`).
- `assert_calendar_in_owner_scope(request.public_api_system_user, org, input.calendar_id)`
  then `Calendar.objects.filter_by_organization(org.id).get(id=input.calendar_id)` →
  `Calendar.DoesNotExist` ⇒ `GraphQLError("Calendar not found.")`.
- Load the event by `(org, calendar_id=input.calendar_id, id=input.event_id)`;
  not found ⇒ `GraphQLError("Event not found.")`.
- **`recurrence_id` present** → `calendar_service.reschedule_event_occurrence(...)` (Phase 0b).
- **absent** → build `CalendarEventInputData` preserving title/description/attendances/
  external_attendances/resource_allocations from the loaded event, override start/end/timezone,
  set `recurrence_rule = input.rrule_string or <existing master's rrule string>`, call
  `calendar_service.update_event(...)`.
- Map `PermissionDenied` / `ValueError` / `DjangoValidationError` / `CalendarIntegrationError`
  to `GraphQLError`; never a 500.

### 4.2 `rescheduleCalendarGroupEvent(input: RescheduleCalendarGroupEventInput!): CalendarEventGraphQLType!`

- Resolve org + init calendar service.
- Load the grouped event by `(org, id=input.event_id)`; derive `primary_calendar_id =
  event.calendar_fk_id`; validate `event.calendar_group_fk_id is not None` else
  `GraphQLError("Event not found.")` (uniform with cross-owner).
- `assert_calendar_in_owner_scope(system_user, org, primary_calendar_id)` (+ uniform
  not-found mapping).
- Wire `deps.calendar_group_service.calendar_service = deps.calendar_service`,
  `initialize(organization=org)`, call
  `reschedule_grouped_event(event_id, start_time, end_time, tz)`; return the updated event.

### 4.3 `cancelEvent(input: CancelEventInput!): CancelEventResult!`

- Resolve org + init calendar service.
- `assert_calendar_in_owner_scope(system_user, org, input.calendar_id)` + load event by
  `(org, calendar_id, event_id)`; uniform `"Calendar not found." / "Event not found."`.
- **`recurrence_id` present** → `calendar_service.cancel_event_occurrence(...)` (Phase 0b).
- **grouped** (`event.calendar_group_fk_id`) → `calendar_group_service.cancel_grouped_event(event_id, delete_series=input.delete_series)`.
- **single** → `calendar_service.delete_event(calendar_id, event_id, delete_series=input.delete_series)`.
- Return `CancelEventResult(success=True)`; map service errors to `GraphQLError`.

**Errors (all three):** `Calendar not found.` (cross-owner / missing calendar — identical to
a real miss), `Event not found.`, plus mapped `PermissionDenied` / validation messages.

## 5. Phased Rollout

### Phase 0a — Service write-allowance for public tokens

**Goal**: `update_event` and `delete_event` accept owner-scoped (own-calendar) and org-wide
public `SystemUser` tokens instead of rejecting all of them. No user-visible surface yet
(no GraphQL field calls it) — scaffolding that unblocks Phases 1–3.

**Feature flag**: none — additive service branch reachable only by `SystemUser` principals
that currently always raise; no existing successful caller is affected.

Changes:
1. [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py):
   add `_public_token_may_write(self, system_user: SystemUser, calendar: Calendar) -> bool`
   — `True` when `scoped_to_membership_user_id is None` (org-wide); else delegate to the
   existing `_scoped_system_user_owns_calendar(system_user, calendar)`.
2. In `update_event` (the `elif isinstance(context.user_or_token, SystemUser):` branch at
   ~line 649): replace the unconditional `raise PermissionDenied(...)` with
   `if not self._public_token_may_write(context.user_or_token, event.calendar): raise
   PermissionDenied("Calendar matching query does not exist.")` and **skip** the
   `can_perform_update` token check for the sanctioned public-token path (mirrors how
   `create_event`'s owner-scoped path bypasses `can_perform_scheduling`).
3. Same change in `delete_event` (~line 1471), using `event.calendar`.
4. Keep bundle-calendar rejection consistent with create (reject bundle writes for scoped
   tokens; org-wide may act per existing service rules).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit** ([calendar_integration/tests/test_calendar_event_service.py](../calendar_integration/tests/test_calendar_event_service.py) or sibling):
  owner-scoped token on **owned** calendar → `update_event`/`delete_event` succeed;
  owner-scoped token on **foreign** calendar → `PermissionDenied`; **org-wide** token on any
  org calendar → succeed; **regression**: `create_event` for an org-wide token still raises
  `PermissionDenied` (unchanged); a Django `User` path still behaves as before.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches a
hot authorization path with subtle owner/org/bundle branching and a regression contract.

**Reusable skills**: `write-tests` (service-layer unit tests).

Acceptance: with the service context initialized to an owner-scoped token that owns calendar
C, `update_event`/`delete_event` on an event in C succeed; a cross-owner token raises
`PermissionDenied`; org-wide tokens succeed org-wide; `create_event` org-wide still blocked.

---

### Phase 0b — Single-occurrence exception service methods

**Goal**: `CalendarEventService` can reschedule or cancel **one occurrence** of a recurring
series addressed by `(calendar_id, master_event_id, recurrence_id)`, under the same
public-token allowance. No GraphQL surface yet.

**Feature flag**: none — new service methods, additive.

Changes:
1. [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py):
   add `reschedule_event_occurrence(self, calendar_id, master_event_id, recurrence_id,
   start_time, end_time, timezone) -> CalendarEvent` — load master (scoped to org), assert
   `_public_token_may_write`, create a modified-occurrence event via the existing
   `create_event`/exception path (`parent_event_id=master`, `is_recurring_exception=True`,
   new times), and link it through `master.create_exception(recurrence_id,
   is_cancelled=False, modified_object=<modified_event>)`. Idempotent on an existing
   exception for that `recurrence_id` (update in place — `create_exception` already upserts).
2. Add `cancel_event_occurrence(self, calendar_id, master_event_id, recurrence_id) -> None`
   — load master, assert `_public_token_may_write`, call
   `master.create_exception(recurrence_id, is_cancelled=True)`.
3. Validate the master is recurring (`is_recurring`) and the calendar matches; raise
   `ValueError` / `CalendarEvent.DoesNotExist` consistently for bad addressing.
4. Expose both via the `CalendarService` facade
   ([calendar_service.py](../calendar_integration/services/calendar_service.py)) delegating
   to `_get_event_service()`, paralleling `update_event` / `delete_event`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: occurrence reschedule creates an `EventRecurrenceException` with
  `is_cancelled=False`, `modified_event` at the new time, `exception_date == recurrence_id`,
  and leaves the master + rule intact; occurrence cancel creates an exception with
  `is_cancelled=True` and `modified_event is None`, master untouched; owner-scope enforced
  (cross-owner master → `PermissionDenied`); non-recurring master → `ValueError`.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` / `gpt-5` (extended thinking) /
`gemini-3-pro`. Recurrence exception construction + linkage is the subtlest logic in the plan.

**Reusable skills**: `write-tests`.

Acceptance: `reschedule_event_occurrence` and `cancel_event_occurrence` produce the correct
`EventRecurrenceException` rows for a single occurrence without mutating the master/series,
and reject cross-owner masters.

---

### Phase 1 — `rescheduleCalendarEvent` mutation

**Goal**: An owner-scoped token can reschedule a single-calendar event (whole-event,
series-preserving, or single-occurrence) on a calendar its owner owns; an org-wide token can
do so org-wide. Returns the updated event.

**Feature flag**: none — brand-new field.

Changes:
1. [public_api/mutations.py](../public_api/mutations.py): add `RescheduleCalendarEventInput`
   and the `reschedule_calendar_event` resolver per **API Design → `rescheduleCalendarEvent`**.
   Preserve non-time fields from the loaded event (mirror
   `rescheduleCalendarEventWithCode`'s merge in
   [calendar_integration/mutations.py:1553-1593](../calendar_integration/mutations.py#L1553-L1593));
   preserve/override the rrule per **Guiding Decisions**; route `recurrence_id` → Phase 0b.
2. [public_api/permissions.py](../public_api/permissions.py): add
   `"rescheduleCalendarEvent": PublicAPIResources.CALENDAR_EVENT` to `FIELD_TO_RESOURCE_MAPPING`.
   (No `PROVIDER_SCOPED_RESOURCES` change — `CALENDAR_EVENT` already present.)
3. Regenerate the public GraphQL schema artifact if the repo checks one in (follow
   `create-graphql-public-query`).

Spec use-case: `rescheduleCalendarEvent` (reschedule a single-calendar event).

Tests:
- **Unit/Integration** ([public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py)
  + [public_api/tests/test_scoping_security.py](../public_api/tests/test_scoping_security.py)):
  scoped token reschedules its own event (times change, non-time fields preserved); **series**
  reschedule preserves the recurrence rule (rule id unchanged, only times move) and with an
  explicit `rruleString` updates it; **single-occurrence** reschedule (`recurrenceId`) creates
  a modified exception and leaves the master intact; **cross-owner denial** — rescheduling
  owner-B's event yields the *same* `"Calendar not found."` as a genuinely missing calendar
  and no mutation occurs (no existence leak); **org-wide** token reschedules any org event.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Multi-branch
resolver (occurrence vs series vs whole) + security tests.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: `rescheduleCalendarEvent` reschedules an owned/org event and returns the updated
`CalendarEventGraphQLType`; a cross-owner attempt returns `"Calendar not found."` with no
write; series rule is preserved unless `rruleString` is supplied; `recurrenceId` reschedules
one occurrence only.

---

### Phase 2 — `rescheduleCalendarGroupEvent` mutation

**Goal**: An owner-scoped token can reschedule a **grouped** event (primary event + linked
non-primary `BlockedTime`s move together) when its owner owns the primary calendar; org-wide
acts org-wide. Whole-event only (no `recurrenceId`).

**Feature flag**: none — brand-new field.

Changes:
1. [public_api/mutations.py](../public_api/mutations.py): add
   `RescheduleCalendarGroupEventInput` and the `reschedule_calendar_group_event` resolver per
   **API Design → `rescheduleCalendarGroupEvent`**. Derive the primary calendar from the
   loaded grouped event, owner-scope-check it, delegate to
   `CalendarGroupService.reschedule_grouped_event`
   ([calendar_group_service.py:656-783](../calendar_integration/services/calendar_group_service.py#L656-L783)),
   wiring `calendar_group_service.calendar_service = calendar_service` + `initialize(org)` as
   `rescheduleCalendarGroupEventWithCode` does.
2. [public_api/permissions.py](../public_api/permissions.py): add
   `"rescheduleCalendarGroupEvent": PublicAPIResources.CALENDAR_EVENT` to the mapping.
3. Regenerate the schema artifact if applicable.

Spec use-case: `rescheduleCalendarGroupEvent` (reschedule a grouped event).

Tests:
- **Integration**: scoped token reschedules its own grouped event — primary event times
  change and the linked `group-event-{id}-cal-*` `BlockedTime`s are updated; **cross-owner
  denial** on the primary calendar → `"Calendar not found."`, no change; **org-wide** token
  reschedules org-wide; a non-grouped `event_id` → `"Event not found."` (no leak).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Group fan-out
+ owner-scope on the derived primary calendar.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: `rescheduleCalendarGroupEvent` moves the grouped event and its non-primary busy
markers for an authorized token; cross-owner/non-group attempts return the uniform not-found
error with no write.

---

### Phase 3 — `cancelEvent` mutation

**Goal**: An owner-scoped token can cancel a single-calendar **or** grouped event — whole
event, whole series (`deleteSeries`), or a single occurrence (`recurrenceId`); org-wide acts
org-wide. Returns `CancelEventResult`.

**Feature flag**: none — brand-new field.

Changes:
1. [public_api/mutations.py](../public_api/mutations.py): add `CancelEventInput`,
   `CancelEventResult`, and the `cancel_event` resolver per **API Design → `cancelEvent`**.
   Branch single vs grouped on `event.calendar_group_fk_id` (mirror
   [cancelEventWithCode](../calendar_integration/mutations.py#L1841-L2015)); route
   `recurrenceId` → `cancel_event_occurrence` (Phase 0b); pass `delete_series` through to
   `delete_event` / `cancel_grouped_event`.
2. [public_api/permissions.py](../public_api/permissions.py): add
   `"cancelEvent": PublicAPIResources.CALENDAR_EVENT` to the mapping.
3. Regenerate the schema artifact if applicable.

Spec use-case: `cancelEvent` (cancel a single-calendar or grouped event).

Tests:
- **Integration**: scoped token cancels its own single event (`success=True`, row gone);
  **series** cancel (`deleteSeries=true`) on a recurring master deletes master + instances +
  exceptions + rule; **single-occurrence** cancel (`recurrenceId`) creates a cancellation
  exception and leaves the master/series intact; **grouped** cancel deletes the primary event
  and its `group-event-*` `BlockedTime`s; **cross-owner denial** → `"Calendar not found."`,
  nothing deleted (no existence leak); **org-wide** token cancels org-wide. Assert the
  recurring-master + `deleteSeries=false` footgun is handled (it deletes that event, not a
  silent series wipe) — and document the behavior in the test.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Single/group
branch + series/occurrence matrix + security tests.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: `cancelEvent` cancels owned/org single and grouped events with correct
series/occurrence semantics and returns `CancelEventResult(success=True)`; cross-owner
attempts return the uniform not-found error with no deletion.

## 6. Risk & Rollout Notes

- **Authorization regression surface (Phase 0a) is the top risk.** `update_event`/
  `delete_event` are on hot write paths shared by the REST viewsets, token viewsets, and the
  `*WithCode` mutations. Those callers use a Django `User` or a `CalendarManagementToken`
  (`str`) principal — **not** a `SystemUser` — so the new branch never fires for them. The
  Phase 0a regression test must assert the `User` and booking-code paths are byte-for-byte
  unchanged, and that org-wide `create_event` stays blocked.
- **Existence-leak parity**: every cross-owner / missing path must raise the *same*
  `"Calendar not found."` (calendar) or `"Event not found."` (event) string as a genuine
  miss. The service-layer guard raises `PermissionDenied` with the calendar-not-found message
  so a race that bypasses the GraphQL guard still cannot leak.
- **Recurring footguns**: passing a recurring master id with `deleteSeries=false` deletes the
  master outright (not one occurrence); single-occurrence ops must go through `recurrenceId`.
  Tests pin this. No silent series strip on reschedule (rule preserved unless `rruleString`
  supplied).
- **No feature flag**: justified because the surface is additive and the only new code path
  is gated behind a principal type (`SystemUser`) that previously universally errored. If
  staged rollout is later desired, the seam `_public_token_may_write` is the single choke
  point to gate.
- **No migrations / locks / partitions / backfill** — nothing to roll back at the DB level.
  Rollback = revert the PRs (each phase is independently revertible; reverting a GraphQL phase
  removes a field, reverting Phase 0a/0b restores the blanket rejection with no orphaned data).
- **Schema artifact**: if the repo checks in a public GraphQL schema snapshot, each GraphQL
  phase regenerates it; CI drift check covers it.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should grouped events ever support `recurrenceId` (per-occurrence) ops? | **No** for v1 — group bookings are single appointments; defer until a recurring-group use-case appears. | Product |
| Should `cancelEvent` reject a recurring **master** id with `deleteSeries=false` (instead of deleting the master) to remove the footgun entirely? | Allow it (documented) for parity with the service, but add a test pinning the behavior; revisit if integrators trip on it. | Eng |
| Should reschedule re-check non-primary calendar availability for group events? | **No** for v1 — mirror the existing `reschedule_grouped_event` limitation; note it in API docs. | Eng |

## 8. Touch List

**Phase 0a**
- Edit [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py) — `_public_token_may_write` + `update_event`/`delete_event` allowance.
- Edit [calendar_integration/tests/test_calendar_event_service.py](../calendar_integration/tests/test_calendar_event_service.py) — allowance + regression unit tests.

**Phase 0b**
- Edit [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py) — `reschedule_event_occurrence`, `cancel_event_occurrence`.
- Edit [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py) — facade delegation for both.
- Edit [calendar_integration/tests/test_calendar_event_service.py](../calendar_integration/tests/test_calendar_event_service.py) — occurrence exception unit tests.

**Phase 1**
- Edit [public_api/mutations.py](../public_api/mutations.py) — `RescheduleCalendarEventInput` + `reschedule_calendar_event`.
- Edit [public_api/permissions.py](../public_api/permissions.py) — `FIELD_TO_RESOURCE_MAPPING["rescheduleCalendarEvent"]`.
- Edit [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py), [public_api/tests/test_scoping_security.py](../public_api/tests/test_scoping_security.py).
- Regenerate public GraphQL schema artifact (if present).

**Phase 2**
- Edit [public_api/mutations.py](../public_api/mutations.py) — `RescheduleCalendarGroupEventInput` + `reschedule_calendar_group_event`.
- Edit [public_api/permissions.py](../public_api/permissions.py) — mapping entry.
- Edit [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) (+ scoping security).
- Regenerate schema artifact (if present).

**Phase 3**
- Edit [public_api/mutations.py](../public_api/mutations.py) — `CancelEventInput`, `CancelEventResult`, `cancel_event`.
- Edit [public_api/permissions.py](../public_api/permissions.py) — mapping entry.
- Edit [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) (+ scoping security).
- Regenerate schema artifact (if present).

## Amendments

- **2026-06-23** — Bundle events must be reschedulable/cancellable through the Public API. Removed the scoped-token bundle-calendar `PermissionDenied` from `update_event` + `delete_event` (Phase 0a) and `_load_recurring_master_for_occurrence` (Phase 0b); bundle **primary** events now flow to `_update_bundle_event` / `_delete_bundle_event`, gated only by `_public_token_may_write`. Flipped the three "bundle blocked" unit tests to "bundle allowed". Added the **Bundle events** row to Guiding Decisions. Affected phases: 0a, 0b (code + tests). Branches force-pushed: phase-0a, phase-0b, and downstream rebases phase-1, phase-2, phase-3.
