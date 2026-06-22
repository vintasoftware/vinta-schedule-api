# Per-Owner-Scoped Public API Token Writes — Implementation Plan

> Continuation of the **Per-Owner-Scoped Public API Tokens** feature. Shares the spec
> [`2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md`](2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md).
> The token model, read scoping, mint mutation, and REST scoped-create (spec use-cases 1, 4, 6 and
> objectives 1–3) shipped as phases 0–3 of
> [`2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_IMPLEMENTATION_PLAN.md`](2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_IMPLEMENTATION_PLAN.md)
> (PRs #105–#107). This plan delivers the remaining **provider write capabilities** (spec objective 4 /
> use-case 2) and the **bypass-surface security sign-off** (objective 5), re-grounded on a `main` that
> has since absorbed the **public-graphql-service-wrappers** feature — which already ships org-wide
> public-API write mutations for blocked times and availability.

## 1. Goals

1. A provider-scoped token can **set recurring availability, add specific available dates, add blocked
   times, and schedule events** through the public GraphQL API, and every such write is confined to the
   token owner's calendars.
2. A provider-scoped token's attempt to **write to another owner's calendar is refused as not-found**,
   revealing nothing about the other owner's data (spec use-case 3).
3. **Existing org-wide tokens are byte-for-byte unchanged** on every write path (spec objective 2). The
   owner guard is a no-op when the token is unscoped.
4. **No reachable field or mutation leaks across owners** — including nested GraphQL field traversal —
   proven by a reviewed bypass-surface checklist (spec objective 5).

**Non-goals:**

- New availability/blocked-time *mutations* — `main` already has them; this plan makes the existing
  ones owner-aware rather than adding parallel `createAvailableTime`/`createBlockedTime` fields (the
  redundant approach from the original plan's phases 4a/4b).
- Single-use scheduling codes and the patient-token write path (spec Negative scope — separately
  planned).
- The `user_created` webhook / provider-creation trigger (spec Negative scope).
- Mutable owner / re-scoping; re-revealing token secrets; object-level per-resource owners (spec
  Negative scope).
- Owner-lifecycle auto-revoke (spec Open questions item 2 — enforcement already denies a deactivated
  owner's data by re-deriving the calendar set per request).
- A feature flag — see **Guiding Decisions**.
- Resource-calendar and bundle mutations (`createResourceCalendar`, `createCalendarBundle`, …) — these
  are org-administration operations, not provider-owned scheduling data, and stay org-wide.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Extend, don't duplicate** | Make `main`'s existing org-wide write mutations owner-aware instead of adding new mutations. Two mutations can't share a GraphQL field name (`createBlockedTime` already exists on `main`), and a parallel surface would double the enforcement area. The owner guard is one shared call added to each existing resolver. |
| **Single enforcement primitive** | Reuse [`scoped_calendar_ids`](../public_api/scoping.py) (already powering read scoping) via a new write-side guard `assert_calendar_in_owner_scope(...)`. `scoped_calendar_ids` returns `None` for org-wide tokens → the guard short-circuits → org-wide behavior is structurally unchanged. One primitive for reads and writes means one place to audit. |
| **Not-found, never forbidden** | A scoped token targeting a calendar outside its owner set gets `Calendar.DoesNotExist` re-wrapped as the *same* "does not exist" error a genuinely missing calendar produces. A cross-owner write must not confirm the target exists (spec use-case 3 / acceptance scenario 3). |
| **Guard every write verb, not just create** | `create` + `update` + `delete` + `batch` all take a `calendar_id` and all are reachable by a token holding the resource. Guarding only `create` would leave `update`/`delete` as cross-owner mutation vectors. Objective 5 requires *no* bypass. |
| **One resource vocabulary** | Add the existing write-resource enums (`CREATE/UPDATE/DELETE_BLOCKED_TIME`, the four availability-window write resources, and `CALENDAR_EVENT`) to `PROVIDER_SCOPED_RESOURCES`. A scoped token authorizes a write with the *same* resource name an org-wide token uses; owner-scope is an orthogonal second layer. No re-mapping of existing mutations → no change to existing callers' grants. |
| **Event creation: narrow sanctioned allowance** | [`CalendarEventService.create_event`](../calendar_integration/services/calendar_event_service.py) hard-blocks all `SystemUser` callers. Relax it *only* for a token whose owner owns the target calendar (verified independently via `CalendarOwnership`, not trusted from the caller). Org-wide tokens stay blocked for event creation — they still route through single-use codes / public scheduling. Bundle calendars are rejected for scoped tokens. |
| **`scheduleEvent` takes an input object** | Every write mutation `main` shipped takes a typed `input:` object. `scheduleEvent` follows suit (`ScheduleEventInput`) for a consistent public-API contract. |
| **No feature flag** | The guard is provably a no-op for org-wide tokens (asserted by flag-off-style regression tests), and scoped tokens are a brand-new class with none in production — there is no existing behavior to gate. Consistent with how phases 1–3 (read scoping) shipped. Cost of a flag here is pure ceremony. |
| **Security sweep is its own phase** | Nested GraphQL field traversal (`event.calendar`, `event.groupSelections.slot.calendars`, recurrence-exception back-pointers, …) bypasses top-level resolver scoping because Strawberry permission/field logic runs only on the decorated field. This is a distinct concern from the write guards and warrants a dedicated adversarial phase + sign-off doc. |

## 3. Data Model Changes

No new tables, columns, or migrations. The owner-scope column (`SystemUser.scoped_to_membership`) and
the enforcement primitive shipped in phases 0–3.

### 3.1 `PROVIDER_SCOPED_RESOURCES` additions

In [public_api/constants.py](../public_api/constants.py), extend the frozenset (currently
`AVAILABLE_TIME, BLOCKED_TIME, CALENDAR_EVENT, AVAILABILITY_WINDOWS, UNAVAILABLE_WINDOWS, CALENDAR`) with
the write-resource enums so a scoped token can be granted them. Added incrementally by phase:

- **Phase 1**: `CREATE_BLOCKED_TIME`, `UPDATE_BLOCKED_TIME`, `DELETE_BLOCKED_TIME`.
- **Phase 2**: `CREATE_AVAILABILITY_WINDOW`, `UPDATE_AVAILABILITY_WINDOW`, `DELETE_AVAILABILITY_WINDOW`,
  `BATCH_UPDATE_AVAILABILITY_WINDOWS`.
- **Phase 3**: `CALENDAR_EVENT` is already present — no addition needed.

The mint mutation `create_scoped_system_user` already validates the requested resource set against
`PROVIDER_SCOPED_RESOURCES`, so each addition automatically becomes grantable to scoped tokens.

### 3.2 Write-side owner-scope guard (new helper)

Add to [public_api/scoping.py](../public_api/scoping.py), beside `scoped_calendar_ids`:

```python
def assert_calendar_in_owner_scope(
    system_user: "SystemUser | None",
    organization: "Organization",
    calendar_id: int,
) -> None:
    """Raise Calendar.DoesNotExist if a scoped token targets a calendar outside its owner set.

    No-op for org-wide tokens (scoped_calendar_ids returns None) and for a None system_user.
    The raised error is indistinguishable from a genuinely missing calendar (no existence leak).
    """
    if system_user is None:
        return
    allowed_ids = scoped_calendar_ids(system_user, organization)
    if allowed_ids is not None and calendar_id not in allowed_ids:
        raise Calendar.DoesNotExist("Calendar matching query does not exist.")
```

Introduced in Phase 1; reused by Phases 2 and 3. Placed in `scoping.py` (not `mutations.py`) to avoid an
import cycle and to keep the read guard
([`_prepare_service_and_calendar`](../public_api/queries.py)) and this write guard side by side for audit.

### 3.3 Type plumbing

- Phase 3 adds `ScheduleEventInput` (Strawberry `@strawberry.input`) and reuses the existing
  `CalendarEventGraphQLType` as the return type. No TypedDict/dataclass changes elsewhere.

## 4. API Design

All mutations live under the public GraphQL schema in [public_api/mutations.py](../public_api/mutations.py)
and are gated `[IsAuthenticated, OrganizationResourceAccess]`. The resource each maps to is unchanged from
`main` except `scheduleEvent` (new).

### 4.1 Owner-aware blocked-time writes (Phase 1)

`createBlockedTime`, `updateBlockedTime`, `deleteBlockedTime` — unchanged signatures and return types.
After resolving the org and reading `input.calendar_id`, call
`assert_calendar_in_owner_scope(system_user, org, input.calendar_id)` before the service call. Scoped
token + foreign calendar → not-found (no row touched). Org-wide token → guard is a no-op.

### 4.2 Owner-aware availability writes (Phase 2)

`createAvailabilityWindow`, `updateAvailabilityWindow`, `deleteAvailabilityWindow`,
`batchUpdateAvailabilityWindows` — same treatment. (These mutations write `AvailableTime` rows: a row
with no `rrule` is a specific available date; a row with an RFC-5545 `rrule` is recurring availability —
together they cover the spec's "recurring availability + specific availability dates".)

### 4.3 `scheduleEvent` (Phase 3)

```
scheduleEvent(input: ScheduleEventInput!): CalendarEventGraphQLType
```

`ScheduleEventInput`: `calendar_id: int`, `start_time`, `end_time`, `timezone: str`, `title: str`,
`description: str = ""`, internal `attendee_user_ids: list[int] = []`, external attendees
(email/name pairs), optional `rrule_string`. Owner-guarded via `assert_calendar_in_owner_scope`;
internal attendees pre-validated as active members of the caller's org; clean GraphQL errors for
`ValueError` / `PermissionDenied` / no-availability. Mapped to `CALENDAR_EVENT`.

Errors: cross-owner / missing calendar → not-found (identical message); bundle calendar for a scoped
token → clean error; org-wide token → blocked (event creation still requires codes / public scheduling);
title too long / out-of-org attendee / malformed rrule → clean GraphQL error, no row.

## 5. Phased Rollout

### Phase 1 — Owner-guard blocked-time writes

**Goal**: A provider-scoped token can create/update/delete blocked times only on its owner's calendars;
org-wide tokens are unchanged.

**Feature flag**: none — guard is a no-op for org-wide tokens; scoped tokens are a new class. Per-phase
regression test asserts org-wide behavior is identical.

Changes:
1. [public_api/scoping.py](../public_api/scoping.py): add `assert_calendar_in_owner_scope` (the shared
   write guard — §Data Model Changes 3.2).
2. [public_api/mutations.py](../public_api/mutations.py): in `create_blocked_time`,
   `update_blocked_time`, `delete_blocked_time`, call the guard right after the org is resolved and
   before the calendar service call; re-wrap `Calendar.DoesNotExist` as the existing not-found error.
3. [public_api/constants.py](../public_api/constants.py): add `CREATE_BLOCKED_TIME`,
   `UPDATE_BLOCKED_TIME`, `DELETE_BLOCKED_TIME` to `PROVIDER_SCOPED_RESOURCES`.

Spec use-case: use-case 2 (blocked-time write) + use-case 3 (cross-owner refused) for blocked times.

Tests:
- **Unit**: [public_api/tests/test_scoping.py](../public_api/tests/test_scoping.py) — `assert_calendar_in_owner_scope` returns/raises correctly for org-wide (None), in-scope, and out-of-scope.
- **Integration**: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) — scoped token creates/updates/deletes a blocked time on its owned calendar (success); cross-owner `calendar_id` on each of the three verbs → not-found *identical* to a genuinely missing calendar AND no `BlockedTime` row created/changed/deleted; **org-wide token writes on any org calendar exactly as before** (the no-regression assertion); missing resource grant → denied.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Surgical but spans three mutations + a shared helper + security-sensitive not-found semantics with adversarial tests.

**Reusable skills**: `write-tests` (mutation integration tests under [public_api/tests/](../public_api/tests/)).

Acceptance: a scoped token's blocked-time create/update/delete succeeds on its owner's calendar and returns not-found (no row touched) on any other calendar; the existing blocked-time mutation suite passes unchanged for org-wide tokens.

### Phase 2 — Owner-guard availability writes

**Goal**: A provider-scoped token can set recurring availability and specific available dates only on its
owner's calendars; org-wide tokens are unchanged.

**Feature flag**: none (same rationale as Phase 1).

Changes:
1. [public_api/mutations.py](../public_api/mutations.py): add `assert_calendar_in_owner_scope` to
   `create_availability_window`, `update_availability_window`, `delete_availability_window`, and
   `batch_update_availability_windows` (guard on `input.calendar_id`, which all four share).
2. [public_api/constants.py](../public_api/constants.py): add `CREATE_AVAILABILITY_WINDOW`,
   `UPDATE_AVAILABILITY_WINDOW`, `DELETE_AVAILABILITY_WINDOW`, `BATCH_UPDATE_AVAILABILITY_WINDOWS` to
   `PROVIDER_SCOPED_RESOURCES`.

Spec use-case: use-case 2 (availability write) + use-case 3 (cross-owner refused) for availability.

Tests:
- **Integration**: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) — scoped token creates a one-off (no rrule) and a recurring (rrule) availability on its owned calendar; update/delete/batch on its owned calendar succeed; cross-owner `calendar_id` on each verb (including each op inside a batch) → not-found, no `AvailableTime` row touched; org-wide token unchanged; missing resource grant → denied. Batch atomicity: a batch mixing an owned and a foreign calendar id is rejected with no partial write.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Four mutations including the atomic batch path; batch needs per-operation guard + all-or-nothing assertions.

**Reusable skills**: `write-tests`.

Acceptance: a scoped token's availability create/update/delete/batch succeeds on its owner's calendar and is not-found on any other; batch writes are all-or-nothing; the existing availability mutation suite passes unchanged for org-wide tokens.

### Phase 3 — `scheduleEvent` mutation + owner-scoped event allowance

**Goal**: A provider-scoped token can schedule events on its owner's calendars through a new
`scheduleEvent` mutation; org-wide event creation stays blocked.

**Feature flag**: none. The service-layer change is *additive* — it opens a new allowed path for
owner-scoped tokens; the existing `SystemUser` block stays in force for org-wide tokens, asserted by test.

Changes:
1. [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py):
   in `create_event`, replace the blanket `SystemUser` `PermissionDenied` with: allow when the
   `SystemUser` is scoped (`scoped_to_membership_fk_id is not None`) **and** an independent
   `CalendarOwnership` lookup confirms the scoped owner owns the target calendar in that org; otherwise
   keep raising. Reject `BUNDLE` calendars for scoped tokens. Guard the post-commit audit side-effect so
   the actor falls back to the `SystemUser` when there is no management token.
2. [public_api/mutations.py](../public_api/mutations.py): add `schedule_event` returning
   `CalendarEventGraphQLType`, owner-guarded via `assert_calendar_in_owner_scope`; pre-validate internal
   attendees are active org members; translate external attendees; map `ValueError` /
   `PermissionDenied` / `NoAvailableTimeWindowsError` to clean GraphQL errors. Add `ScheduleEventInput`.
3. [public_api/permissions.py](../public_api/permissions.py): map `scheduleEvent → CALENDAR_EVENT`
   (already in `PROVIDER_SCOPED_RESOURCES`).

Spec use-case: use-case 2 (schedule-event write) + use-case 3 (cross-owner refused) for events.

Tests:
- **Integration**: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) — scoped owner schedules on a default (non-public) owned calendar with a covering availability window → success; recurring; internal + external attendees persisted; out-of-org attendee → clean error, no row; cross-owner calendar → not-found identical to missing + no row; **org-wide token → denied, no row** (the event-creation-still-blocked assertion); bundle calendar → clean error, no row; no availability window → clean error; missing `CALENDAR_EVENT` grant → denied.
- **Unit**: [calendar_integration/tests/](../calendar_integration/tests/) — `create_event` allows an owner-scoped `SystemUser` on an owned calendar and still raises for an org-wide `SystemUser` and for a scoped token on a non-owned calendar (independent `CalendarOwnership` verification).

**Suggested AI model**: Tier 4 — `claude-opus-4-7`. Touches the event-creation service authorization (the most security-sensitive path), a new mutation, attendee validation, and the post-commit audit actor; the original attempt at this had a real BLOCKER caught in review.

**Reusable skills**: `graphql-public-query` (the new mutation); `write-tests`.

Acceptance: a scoped token schedules an event only on its owner's calendars; an org-wide token's `scheduleEvent` is denied with no row; cross-owner is not-found with no row; `create_event` independently verifies ownership rather than trusting the caller.

### Phase 4 — Nested-field owner-scope sweep + security review

**Goal**: Prove (and where needed, close) that no scoped token can reach another owner's data through
nested GraphQL field traversal on any reachable type, and ship the bypass-surface sign-off.

**Feature flag**: none.

Changes:
1. [calendar_integration/graphql.py](../calendar_integration/graphql.py): add owner-scoped
   `@strawberry.field` resolvers driven by an `_owner_scoped_calendar_ids(info)` helper that returns
   `None` for org-wide *and* internal (non-public-API) requests — so those paths stay byte-for-byte —
   or the owner's calendar-id set for a scoped token. Guard the cross-owner-reachable nested fields on
   `CalendarEventGraphQLType` (`calendar`, `bundleCalendar`, `bundlePrimaryEvent`,
   `bundleRepresentations`, `bulkModifications`, `recurringInstances`, `resources`,
   `resourceAllocations`, `groupSelections`, `calendarGroup`), the **second-hop**
   `groupSelections.slot` (+ `CalendarGroupSlot.calendars` pool), `BlockedTimeGraphQLType` /
   `AvailableTimeGraphQLType` `calendar` + recurrence-exception back-pointers, and
   `EventExternalAttendance.event`. Filter to the owner's calendars / suppress for scoped tokens;
   unchanged for org-wide.
2. New: `ai-plans/2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_SECURITY_REVIEW.md` — the
   bypass-surface checklist: every reachable read/write field, its guard mechanism, and the proving
   test. Sign-off artifact (mirrors the original plan's review doc).

Spec use-case: use-case 3 (consolidated negative-path guarantee) + objective 5 (no bypass).

Tests:
- **Integration**: [public_api/tests/test_scoping_security.py](../public_api/tests/test_scoping_security.py)
  (new) — for every reachable read field AND each provider write mutation: no cross-owner top-level
  leak, **no cross-owner leak via nested-field selection** (queries explicitly select the nested
  fields), cross-owner writes denied with no row, and an org-wide regression assertion per guarded
  field. Each test fails if its guard is reverted.

**Suggested AI model**: Tier 4 — `claude-opus-4-7`. Adversarial completeness work; the original sweep
missed a second-hop leak (`groupSelections.slot.calendars`) that only an adversarial pass caught.

**Reusable skills**: `write-tests`.

Acceptance: a scoped token selecting any nested field on any returned object sees only its owner's data;
org-wide and internal GraphQL consumers of the shared types are byte-for-byte unchanged; the
security-review checklist enumerates every reachable field with its guard + proving test and is signed
off.

## 6. Risk & Rollout Notes

- **No feature flag.** Enforcement is a no-op for org-wide tokens (`scoped_calendar_ids` → `None`) and
  scoped tokens are new; every phase ships a regression test asserting org-wide writes are unchanged.
  Rollback = revert the phase's PR; nothing persisted needs unwinding.
- **No migration, no backfill, no locks.** Pure code; the owner-scope column shipped in phase 0.
- **Ordering / dependency.** This plan stacks on phase-3 of the original plan (PR #107 — read scoping +
  mint). Within this plan, Phases 1→2→3 are independent of each other (different mutations) and may land
  in any order; Phase 4 is independent of 1–3 (it touches `graphql.py` nested resolvers, not the write
  mutations) and could even land first. **Phase 4 closes a nested-read leak that becomes live the moment
  the original plan's read-scoping (PRs #105–#107) merges with scoped tokens in use** — prioritize it
  alongside the write phases, don't leave it for last in wall-clock terms even though it's numbered last.
- **Event-service blast radius (Phase 3).** `create_event` is shared by internal callers, not just the
  public API. The change must be strictly additive for the existing `SystemUser`-blocked path and leave
  every non-`SystemUser` caller untouched — covered by the `calendar_integration` regression suite, which
  must pass unchanged.
- **Query-plan note.** `scoped_calendar_ids` runs one indexed `Calendar` query per guarded request for
  scoped tokens only; org-wide tokens skip it. No hot-path regression for existing traffic.
- **Rollback story.** Each phase is an independent revert. Reverting Phase 3 also requires the
  `create_event` change to revert cleanly (it's a localized branch) — keep it isolated from unrelated
  service edits.

## 7. Open Questions

1. **Exact provider resource set the bot mints** (spec Open questions item 1). Recommended default: grant
   the provider token the read resources (shipped) **plus** the blocked-time + availability write
   resources added here, plus `CALENDAR_EVENT`. Owner: Medplum integration owner. Unblocks: nothing in
   this plan — the allow-list additions make all of them *grantable*; which subset the bot actually
   requests is a client decision.
2. **Should the batch availability mutation reject or skip a foreign-calendar operation?** Recommended
   default: **reject the whole batch** (atomic, no partial write) to match the spec's not-found-on-write
   guarantee and the mutation's existing atomic semantics. Owner: eng. Resolved as the Phase 2 default
   unless overridden.
3. **Auto-revoke on owner deactivation** (spec Open questions item 2). Recommended default: no — per
   the spec, per-request re-derivation already denies a dead owner's data. Owner: platform security.
   Out of scope here.

## 8. Touch List

**Phase 1 — blocked-time write scoping**
- Edit: [public_api/scoping.py](../public_api/scoping.py) (add `assert_calendar_in_owner_scope`)
- Edit: [public_api/mutations.py](../public_api/mutations.py) (`create/update/delete_blocked_time`)
- Edit: [public_api/constants.py](../public_api/constants.py) (3 resources → `PROVIDER_SCOPED_RESOURCES`)
- Edit: [public_api/tests/test_scoping.py](../public_api/tests/test_scoping.py)
- Edit: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py)

**Phase 2 — availability write scoping**
- Edit: [public_api/mutations.py](../public_api/mutations.py) (`create/update/delete_availability_window`, `batch_update_availability_windows`)
- Edit: [public_api/constants.py](../public_api/constants.py) (4 resources → `PROVIDER_SCOPED_RESOURCES`)
- Edit: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py)

**Phase 3 — scheduleEvent + service allowance**
- Edit: [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py) (`create_event` owner-scoped allowance)
- Edit: [public_api/mutations.py](../public_api/mutations.py) (`schedule_event` + `ScheduleEventInput`)
- Edit: [public_api/permissions.py](../public_api/permissions.py) (`scheduleEvent → CALENDAR_EVENT`)
- Edit: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py)
- Edit: [calendar_integration/tests/](../calendar_integration/tests/) (service-level authorization tests)
- Regenerate: `@schema.yml`

**Phase 4 — nested-field sweep + security review**
- Edit: [calendar_integration/graphql.py](../calendar_integration/graphql.py) (owner-scoped nested resolvers + `_owner_scoped_calendar_ids`)
- New: `@public_api/tests/test_scoping_security.py`
- New: `@ai-plans/2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_SECURITY_REVIEW.md`
- Regenerate: `@schema.yml`
