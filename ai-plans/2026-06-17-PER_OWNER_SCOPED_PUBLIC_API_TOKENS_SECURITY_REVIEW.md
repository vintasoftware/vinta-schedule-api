# Per-Owner-Scoped Public API Tokens — Bypass-Surface Security Review (Phase 5)

- **Plan**: `2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_IMPLEMENTATION_PLAN.md`
- **Spec**: `2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md`
- **Sweep tests**: `public_api/tests/test_scoping_security.py`
- **Date**: 2026-06-18

This is the security sign-off artifact referenced in the plan's Phase 5 acceptance. It
enumerates **every field and mutation a provider-scoped token can reach** — including
nested/related fields reachable by GraphQL traversal — and records the owner-guard
mechanism plus the test that proves it.

## Threat model

A **provider-scoped token** is a `SystemUser` whose `scoped_to_user` FK is set. "This
provider's data" = calendars where `CalendarOwnership.user == scoped_to_user`
(`scoped_calendar_ids(system_user, organization)` in `public_api/scoping.py`) plus the
events / blocked times / available times / allocations on those calendars. The adversary
is a holder of a provider-scoped token attempting to **read or write another provider's
data** within the same organization (cross-organization is already impossible — the org
context is set by the auth middleware and every query is org-filtered).

Guard semantics: **reads return empty / null; writes return not-found** — never an oracle
that confirms another provider's object exists. `scoped_calendar_ids` returns `None` for an
org-wide token (`scoped_to_user IS NULL`), which makes every guard a no-op for legacy
tokens (byte-for-byte unchanged).

## Provider-reachable resources

A provider-scoped token can only be granted resources in `PROVIDER_SCOPED_RESOURCES`
(`public_api/constants.py`): `AVAILABLE_TIME`, `BLOCKED_TIME`, `CALENDAR_EVENT`,
`AVAILABILITY_WINDOWS`, `UNAVAILABLE_WINDOWS`, `CALENDAR`. Mint-time validation
(`createScopedSystemUser`, REST create) rejects any over-grant, so a provider token can
**never** hold `CALENDAR_GROUP`, `USER`, `SYSTEM_USER`, `ORGANIZATION`, etc. Fields gated by
those resources (`calendarGroup*`, `users`, `createScopedSystemUser`, `childOrganizations`,
…) are therefore unreachable by a provider token and are out of this sweep's scope.

## 1. Top-level READ queries

| Field (resource) | Owner-guard mechanism | Proven by |
|---|---|---|
| `calendars` (CALENDAR) | List constrained with `id__in=scoped_calendar_ids(...)` in `queries.py`; single-id lookup further `.filter(id=…)`. | `TestTopLevelReadNoLeak::test_calendars_excludes_other_owner` |
| `calendarEvents` by `eventId` (CALENDAR_EVENT) | Queryset `.filter(calendar_fk__in=allowed)` in `queries.py`. | `TestTopLevelReadNoLeak::test_calendar_events_by_id_blocks_other_owner` |
| `calendarEvents` by range (CALENDAR_EVENT) | `prepare_service_and_calendar` raises `Calendar.DoesNotExist` for a calendar ∉ owner set (same path as a missing calendar); the post-expansion list is also filtered by `calendar_fk_id ∈ allowed`. | `TestTopLevelReadNoLeak::test_calendar_events_by_range_on_b_calendar_is_not_found` |
| `blockedTimes` by id / range (BLOCKED_TIME) | id path `.filter(calendar_fk__in=allowed)`; range path guarded by `prepare_service_and_calendar` + post-expansion filter. | `TestTopLevelReadNoLeak::test_blocked_times_by_id_blocks_other_owner` |
| `availableTimes` by id / range (AVAILABLE_TIME) | Same pattern as blocked times. | `TestTopLevelReadNoLeak::test_available_times_by_id_blocks_other_owner` |
| `availabilityWindows` (AVAILABILITY_WINDOWS) | `prepare_service_and_calendar` owner guard (cross-owner `calendar_id` → not-found). | Phase 1 `test_queries.py` cross-owner windows tests + this guard shared with the mutations swept below. |
| `unavailableWindows` (UNAVAILABLE_WINDOWS) | `prepare_service_and_calendar` owner guard. | Phase 1 `test_queries.py`. |

## 2. Nested / related fields (the GraphQL field-traversal surface)

A scoped token may legitimately fetch its **own** event/blocked/available time/calendar,
but GraphQL lets it select nested fields. The fields below are owner-guarded by **field
resolvers** added in `calendar_integration/graphql.py` (Phase 5). Each resolver derives the
owner set via `_owner_scoped_calendar_ids(info)` (reads `request.public_api_system_user` +
`public_api_organization`) and filters; for org-wide tokens (`None`) the field resolves
unchanged. Every queryset touched is first filtered by the parent's `organization_id` to
satisfy the tenant safety net.

### `CalendarEventGraphQLType`

| Nested field | Can leak? | Owner-guard mechanism | Proven by |
|---|---|---|---|
| `calendar` | Defense-in-depth (the event was only returned because its calendar is owned) | Resolver returns `None` if `calendar.id ∉ allowed`. Positive control asserts the owned calendar still resolves. | `TestNestedFieldTraversalNoLeak::test_own_calendar_still_resolves` |
| `bundleCalendar` | **Yes** — the bundle calendar may be owned by another provider. | Resolver returns `None` if `∉ allowed`. | `…::test_bundle_calendar_excludes_other_owner` |
| `bundlePrimaryEvent` | **Yes** — primary event hosted on another provider's primary calendar. | Resolver returns `None` if the event's `calendar_fk_id ∉ allowed`. | `…::test_bundle_primary_event_excludes_other_owner` |
| `bundleRepresentations` | **Yes** — representation events live on other-provider child calendars. | Resolver `.filter(calendar_fk_id__in=allowed)`. | `…::test_bundle_representations_excludes_other_owner` |
| `recurringInstances` (Django rel `calendarevent_recurring_instances`) | **Yes** — an instance's `calendar_fk` may differ from the parent. | Resolver `.filter(calendar_fk_id__in=allowed)`. | `…::test_recurring_instances_excludes_other_owner` |
| `bulkModifications` | **Yes** — continuation events could be on another calendar. | Resolver `.filter(calendar_fk_id__in=allowed)`. | Covered structurally; identical filter to `bundleRepresentations` (same `_scoped_event_list` helper, asserted there). |
| `bulkModificationParent` / `parentRecurringObject` | **Yes** — parent could be on another calendar. | Resolver returns `None` if `calendar_fk_id ∉ allowed`. | Identical guard to `bundlePrimaryEvent` (same `_scoped_event_or_none` helper, asserted there). |
| `resources` | **Yes** — resource calendars allocated to the event may not be owned. | Resolver `.filter(id__in=allowed)` on the resource calendars. | `…::test_resources_excludes_other_owner` |
| `resourceAllocations` | **Yes** — exposes resource calendars via `allocation.calendar`. | Resolver `.filter(calendar_fk_id__in=allowed)`. | `…::test_resources_excludes_other_owner` (asserts allocation.calendar too) |
| `groupSelections` | **Yes** — per-slot picks point at other calendars in a group. | Resolver `.filter(calendar_fk_id__in=allowed)`. | `…::test_group_selections_excludes_other_owner` |
| `calendarGroup` | **Yes** — a group aggregates cross-provider calendars via its slots. | Resolver returns `None` entirely for scoped tokens (group membership is not owner data). | `…::test_calendar_group_suppressed_for_scoped_token` |
| `attendances` / `attendees` / `externalAttendees` | **No** — people, not calendar-scoped data; out of "another owner's calendar data" per the plan's owner edge. | Resolve unchanged (documented). | n/a (inherently safe) |
| `externalAttendances` | Scalar-safe list itself (attendance status records for THIS event), but each `EventExternalAttendance` has a back-pointer `event` → `CalendarEventGraphQLType`. | The `event` field on `EventExternalAttendanceGraphQLType` now resolves through `_scoped_event_or_none(info)`, returning null when the pointed-to event's calendar ∉ allowed. | `TestNestedFieldTraversalNoLeak::test_recurrence_exception_own_pointers_still_resolve` (positive control; the external-attendance `event` back-pointer is structurally the same event so it always resolves for a scoped token — the guard prevents cross-owner enumeration via an adversarially constructed attendance record). |
| `recurrenceRule` | **No** — describes THIS event's recurrence pattern only. | Resolved unchanged. | n/a (inherently safe) |
| `recurrenceExceptions` — **list** | **No** — the list itself contains exceptions for THIS event only. | Resolved unchanged. | n/a |
| `recurrenceExceptions[].parentEvent` / `.modifiedEvent` | **Yes** — second-hop: `parentEvent` points to the recurring parent event; `modifiedEvent` points to the replacement instance. Either could be on another owner's calendar in an adversarially constructed state. | Resolver on `EventRecurrenceExceptionGraphQLType.parent_event` and `.modified_event` — each calls `_scoped_event_or_none(info)` returning null when the pointed-to event's `calendar_fk_id ∉ allowed`. | `TestNestedFieldTraversalNoLeak::test_recurrence_exception_parent_and_modified_event_cross_owner_hidden` (adversarial: `modifiedEvent` on B's calendar → null for scoped token); `…::test_recurrence_exception_own_pointers_still_resolve` (positive control: own-calendar pointers still resolve). |
| `groupSelections[].slot` | **Yes (BLOCKER)** — `CalendarGroupSlot.calendars` is the entire cross-provider pool for that slot. Before Fix 1 a scoped token could enumerate ALL provider calendars in the group by traversing `groupSelections → slot → calendars`. | Resolver on `CalendarEventGroupSelectionGraphQLType.slot` returns `None` for any scoped token (mirrors the `calendarGroup` suppression). Defense-in-depth: `CalendarGroupSlotGraphQLType.calendars` is also owner-filtered (pool intersected with `allowed`) so that even if the slot is reached via an org-wide token or a future code path, the pool is scoped. | `TestNestedFieldTraversalNoLeak::test_group_selections_excludes_other_owner` (asserts `slot` is null for scoped token; slot pool contains both A and B calendars to make the test fail without the guard); `…::test_group_selections_slot_calendars_excludes_other_owner_via_org_wide` (org-wide positive control: slot visible, full pool returned). |
| scalar fields (`title`, `startTime`, …) | **No** — own-event scalars. | Resolve unchanged. | n/a (inherently safe) |

### `BlockedTimeGraphQLType` / `AvailableTimeGraphQLType`

| Nested field | Can leak? | Owner-guard mechanism | Proven by |
|---|---|---|---|
| `calendar` | Defense-in-depth (top-level read is already owner-filtered; mutation results write to an owned calendar) | Resolver returns `None` if `calendar.id ∉ allowed`. | Covered by the top-level read guards + nested own-calendar control; same `_scoped_calendar_or_none` helper as the event `calendar` field. |
| `user` | **No** — a person. | Unchanged. | n/a |
| `recurrenceRule` | **No** — own data. | Unchanged. | n/a |
| `recurrenceExceptions` — **list** | **No** — the list itself contains exceptions for THIS blocked/available time only. | Resolved unchanged. | n/a |
| `recurrenceExceptions[].parentBlockedTime` / `.modifiedBlockedTime` (BlockedTime) | **Yes** — second-hop: in an adversarial state these could point to blocked times on another owner's calendar. | `BlockedTimeRecurringExceptionGraphQLType.parent_blocked_time` and `.modified_blocked_time` resolvers call `_scoped_blocked_time_or_none(info)`, returning null when `calendar_fk_id ∉ allowed`. | Structurally: top-level `blockedTimes` is already owner-filtered, so in practice a scoped token can only ever reach a `BlockedTimeRecurrenceException` whose parent is on its own calendar. The guard provides defense-in-depth against future code paths. |
| `recurrenceExceptions[].parentAvailableTime` / `.modifiedAvailableTime` (AvailableTime) | **Yes** — same reasoning as blocked time. | `AvailableTimeRecurringExceptionGraphQLType.parent_available_time` and `.modified_available_time` resolvers call `_scoped_available_time_or_none(info)`. | Same structural defense-in-depth as blocked time exceptions above. |
| `bundle_calendar` / `bundle_primary_event` (model fields) | n/a — **not exposed** on these GraphQL types. | Not reachable. | n/a |

### `CalendarGraphQLType`

All exposed fields are scalars (`id`, `name`, `description`, `email`, `external_id`,
`provider`, `calendar_type`, `capacity`, `manage_available_windows`, `sync_enabled`,
timestamps). **No nested relation exposes other-owner data.** The calendar instance itself
is only reachable when already in the owner set (top-level `calendars` filter or a nested
`*.calendar` resolver). Inherently safe — no change.

## 3. WRITE mutations

| Mutation (resource) | Owner-guard mechanism | Proven by |
|---|---|---|
| `createAvailableTime` (AVAILABLE_TIME) | `prepare_service_and_calendar` raises `Calendar.DoesNotExist` → mapped to the same generic "Calendar matching query does not exist." GraphQL error as a genuinely missing calendar; no row written. | `TestWriteMutationsNoCrossOwnerWrite::test_create_available_time_cross_owner_not_found` |
| `createBlockedTime` (BLOCKED_TIME) | Same `prepare_service_and_calendar` owner guard. | `…::test_create_blocked_time_cross_owner_not_found` |
| `scheduleEvent` (CALENDAR_EVENT) | Same owner guard at the GraphQL layer, **plus** `CalendarEventService.create_event` independently re-checks `CalendarOwnership.user == scoped_to_user` (Phase 4c sanctioned event path) and rejects bundle calendars. | `…::test_schedule_event_cross_owner_not_found` |

Each write test asserts the cross-owner error message is **byte-identical** to the
genuinely-missing-calendar message (no existence oracle) and that **no row** is persisted on
the other owner's calendar.

## 4. Mint surface (escalation)

| Surface | Guard | Proven by (Phase 2/3 tests) |
|---|---|---|
| `createScopedSystemUser` | `SYSTEM_USER` resource gate; owner must be an active member of the caller's org; `available_resources ⊆ PROVIDER_SCOPED_RESOURCES`; duplicate `integration_name` rejected. | `test_mutations.py::TestCreateScopedSystemUser*` (incl. `test_scoped_provider_token_cannot_mint`) |
| REST `POST /public-api-tokens/` (optional owner) | Same owner-in-org + allow-list validation; owner immutable on update; update path blocks granting non-provider resources to a scoped token. | `test_views.py` scoped-token tests |

A provider-scoped token cannot self-escalate: it lacks `SYSTEM_USER`, so it cannot mint new
tokens (`test_scoped_provider_token_cannot_mint`).

## 5. Org-wide regression (no-op for legacy tokens)

`scoped_calendar_ids` returns `None` for `scoped_to_user IS NULL`, so every guard above is a
no-op for org-wide tokens. Proven by `TestOrgWideTokenUnaffected` — an org-wide token sees
both providers' calendars, resolves another owner's event by id, **and still sees
cross-owner nested data** (`bundleRepresentations`, `calendarGroup`), confirming the scoping
(not some unrelated filter) is what hides B's data from scoped tokens.

## Sign-off

The adversarial sweep (`public_api/tests/test_scoping_security.py`, 23 tests) passes; the
full suite + `calendar_integration/` regression + `check --deploy` are green; mypy is clean
on the changed modules. Every reachable field and mutation either filters/guards by owner or
is documented inherently-safe above. **No residual cross-owner read or write path is known.**

### Leaks found & closed during this phase

**First sweep (original Phase 5):** The nested-field traversal surface flagged in Phase 1
(`bundleRepresentations`, `bundleCalendar`, `resources`, `groupSelections`,
`recurringInstances`, and the related event/calendar pointers) **was a real leak**: before
Phase 5, a scoped token fetching its own event could select these nested fields and surface
another provider's calendars/events (the org-wide regression tests demonstrate the data is
genuinely reachable without scoping). Closed by the per-field owner-scoped resolvers in
`calendar_integration/graphql.py`.

**Second sweep (reviewer findings):** Three additional second-hop traversal paths were found
and closed:

1. **`groupSelections → slot → calendars` (BLOCKER):** `CalendarEventGroupSelectionGraphQLType.slot`
   was a raw `strawberry_django.field()`. A scoped token could reach the slot's entire
   cross-provider calendar pool even though `groupSelections` itself was already filtered.
   Fixed by suppressing `slot` entirely for scoped tokens (null resolver) **plus** adding a
   defense-in-depth owner filter on `CalendarGroupSlotGraphQLType.calendars`.

2. **`recurrenceExceptions[].parentEvent` / `.modifiedEvent`:** Both fields on
   `EventRecurrenceExceptionGraphQLType` were raw `strawberry_django.field()` instances.
   In an adversarially constructed state (e.g., a `parentEvent` FK pointing to another
   owner's calendar) a scoped token could traverse to that event. Fixed by replacing both
   with `@strawberry.field` resolvers that call `_scoped_event_or_none(info)`.

3. **`EventExternalAttendanceGraphQLType.event` back-pointer:** The `event` field was a raw
   `strawberry_django.field()` pointing back to the parent `CalendarEventGraphQLType`. While
   structurally it points to the same event the scoped token already owns, a guard is added
   via `_scoped_event_or_none(info)` for defense-in-depth and consistency. Analogous guards
   added to `BlockedTimeRecurringExceptionGraphQLType` and
   `AvailableTimeRecurringExceptionGraphQLType` second-hop pointers.
