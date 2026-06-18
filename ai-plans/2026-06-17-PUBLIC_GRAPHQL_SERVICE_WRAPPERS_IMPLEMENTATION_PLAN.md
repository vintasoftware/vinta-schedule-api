# Public GraphQL Service Wrappers — Implementation Plan

Thin Public GraphQL wrappers over already-existing calendar/availability/bundle services, following the `create-graphql-public-query` pattern. Source of proposed signatures: [building-blocks-integration-v2.md](../docs/building-blocks-integration-v2.md), "What needs to be done → A. Thin GraphQL wrappers". This plan covers **only** section A (the 🔶 items) plus the true service gaps those wrappers expose.

## 1. Goals

1. Expose the existing single-calendar event, resource-calendar, availability/blocked-time, and bundle services through the Public GraphQL API as `IsAuthenticated` + `OrganizationResourceAccess`-guarded fields, each delegating to a DI-injected service and returning the project's `success` / `errorMessage` / payload result shape.
2. Give every new field its **own** `PublicAPIResources` value and `FIELD_TO_RESOURCE_MAPPING` entry, so a token can be granted each query/mutation independently (per the chosen granular-permission model).
3. Add the small service methods the wrappers require but that do **not** exist today: `disable_resource_calendar`, `disable_bundle_calendar`, `update_blocked_time`, `delete_blocked_time`, and `reschedule_grouped_event`.
4. Add a `CalendarBundleGraphQLType` + `calendarBundles` query so bundles (Calendars with `calendar_type=BUNDLE`) become a first-class Public API read surface.

**Scope change (2026-06-18):** Phases 1a/1b/1c (single-calendar event create / reschedule / cancel) are **DROPPED**. `CalendarEventService.create_event` / `update_event` raise `PermissionDenied("Events cannot be created through the Public API.")` for `SystemUser` callers (calendar_event_service.py:246 and :447) — a deliberate guard. Per owner decision, the guard is authoritative: single-event mutations are NOT exposed via the Public API, and `docs/building-blocks-integration-v2.md` is wrong on this point. Phase 5 (group-event reschedule) is flagged to revisit, since `reschedule_grouped_event` would also route through `update_event`.

**Non-goals** (each is a separate, independently-planned change — explicitly out of scope here):
- **Single-calendar event mutations** (`createCalendarEvent`, `rescheduleCalendarEvent`, `cancelEvent`) — blocked by the `SystemUser` guard in `CalendarEventService`; dropped per owner decision 2026-06-18.
- The `is_private` / restricted flag on calendars / groups / bundles.
- The `owners` field on `CalendarGraphQLType`.
- The `userId` argument on the `calendarEvents` query.
- Single-use scheduling / booking codes and the `*WithCode` patient mutations (see [2026-06-17-SINGLE_USE_SCHEDULING_CODES_SPEC.md](2026-06-17-SINGLE_USE_SCHEDULING_CODES_SPEC.md)).
- Per-user / patient-scoped Public API tokens (see [2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md](2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md)).
- `updateResourceCalendar` / `editResourceCalendar` — not requested; also a true service gap (no `update_resource_calendar`). Left out deliberately.
- No new UI — this is API surface for external integrators (Medplum / Building Blocks). No E2E phases.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Surface** | Public GraphQL only, wired on `@public_api/queries.py` and `@public_api/mutations.py`. No REST, no internal schema changes. |
| **Auth** | Existing Public API org token (`Authorization: Bearer {system_user.id}:{token}`), enforced by `IsAuthenticated` + `OrganizationResourceAccess`. No new auth path (that's the codes/scoped-token specs). |
| **Permission granularity** | **One dedicated `PublicAPIResources` value per new field** (e.g. `CREATE_CALENDAR_EVENT`, `RESCHEDULE_CALENDAR_EVENT`, `CANCEL_EVENT`, …). Chosen over reusing the read resources so a token can grant each mutation independently and read grants never imply write. Each value added in the phase that introduces its field. |
| **Service delegation** | Reuse the `calendar_service` DI provider (the facade already exposes events CRUD, `create_resource_calendar`, bundle create/update, `create_available_time`, `create_blocked_time`, `batch_modify_available_times`). `calendar_group_service` for group reschedule. No new DI registrations needed — sub-services (`CalendarEventService`, `CalendarBundleService`, `AvailabilityService`, `CalendarSyncService`) are owned by the facade. |
| **Init pattern** | Mutations resolve org from `info.context.request.public_api_organization`, then `calendar_service.initialize_without_provider(user_or_token=request.public_api_system_user, organization=org)` — mirroring `_prepare_service_and_calendar` in queries and `create_calendar_group_event` in mutations. |
| **Result shape** | Per-field `@strawberry.type` results with `success: bool`, `error_message: str | None`, and an optional payload field (`event` / `calendar` / `bundle` / `available_time` / `blocked_time`). Service exceptions caught → `success=False`. Matches existing `CalendarGroupResult` / `WebhookSubscriptionResult`. |
| **True gaps build in-plan** | `disable_resource_calendar`, `disable_bundle_calendar`, `update_blocked_time`, `delete_blocked_time` added as small service methods folded into their wrapper phase. `reschedule_grouped_event` is architectural → its own foundation phase before the wrapper. |
| **No feature flag** | Purely additive new GraphQL fields no existing code reads/writes; exposure is already opt-in per token via the new resource grants. No flag, no flag-removal phase. (Confirmed in Step 0.) |
| **Disable semantics** | "Disable" = set `Calendar.visibility = INACTIVE` (org-scoped, validated) via a service method. No row deletion. |

### Doc discrepancies flagged (signatures that differ from the v2 doc)

- **Batch op key**: `batch_modify_available_times` operations use `"action"` (`create`/`update`/`delete`), **not** `"op"` as the doc shows. Also only available times are batchable — there is **no** batch path for blocked times.
- **Event resource allocations**: `CalendarEventInputData.resource_allocations` items are `ResourceAllocationInputData(resource_id: int)`, **not** `{ calendarId }` as the doc's `createCalendarEvent` example implies.
- **`rescheduleCalendarGroupEvent`**: doc tags it 🔶 (service ready). **Reality: ❌** — `CalendarGroupService` has no reschedule/update-group-event method. New service method required (Phase 5a).
- **`updateBlockedTime` / `deleteBlockedTime`**: doc tags 🔶. **Reality: ❌** — `AvailabilityService` has `create_blocked_time` + recurring-exception methods only; no single update/delete. New methods required (Phases 3g/3h).
- **`disableResourceCalendar` / `disableCalendarBundle`**: no service method exists; only the `visibility` field. New methods added (Phases 2b/4d).

## 3. Data Model Changes

No new tables, columns, or migrations. The only "model layer" work is **service methods** (Python, no schema change):

### 3.1 New service methods (added in their phases)
- `CalendarService.disable_resource_calendar(calendar_id) -> Calendar` — Phase 2b.
- `CalendarService.disable_bundle_calendar(bundle_id) -> Calendar` — Phase 4d.
- `AvailabilityService.update_blocked_time(calendar, blocked_time_id, ...)` + facade delegate — Phase 3g.
- `AvailabilityService.delete_blocked_time(calendar, blocked_time_id, delete_series=False)` + facade delegate — Phase 3h.
- `CalendarGroupService.reschedule_grouped_event(...)` — Phase 5a.

### 3.2 New constants
- New `PublicAPIResources` values in [public_api/constants.py](../public_api/constants.py), one per field — added incrementally per phase. Full list in Touch List.

### 3.3 Type plumbing
- `CalendarBundleGraphQLType` (new) in `@calendar_integration/graphql.py` — Phase 4a.
- Reuse existing `CalendarEventGraphQLType`, `CalendarGraphQLType`, `AvailableTimeGraphQLType`, `BlockedTimeGraphQLType` for payloads.
- Mutation input `@strawberry.input` types per field (mirroring `CalendarGroupInput` etc.).

## 4. API Design

All fields follow the same contract: `permission_classes=[IsAuthenticated, OrganizationResourceAccess]`, a single `input` argument (mutations) or scalar args (queries), org resolved from request context, service delegation, `{ success, errorMessage, <payload> }` result. Concrete proposed signatures live in [building-blocks-integration-v2.md](../docs/building-blocks-integration-v2.md) ("Integration touch-points per screen"); this plan implements them with the corrections noted in Guiding Decisions.

Mutations to add: `createCalendarEvent`, `rescheduleCalendarEvent`, `cancelEvent`, `createResourceCalendar`, `disableResourceCalendar`, `importResourceCalendars`, `createAvailabilityWindow`, `updateAvailabilityWindow`, `deleteAvailabilityWindow`, `batchUpdateAvailabilityWindows`, `createBlockedTime`, `updateBlockedTime`, `deleteBlockedTime`, `createCalendarBundle`, `updateCalendarBundle`, `disableCalendarBundle`, `rescheduleCalendarGroupEvent`.

Queries to add: `calendarBundles`.

## 5. Phased Rollout

Phases are MR-sized, one use-case each, independently mergeable, each with its own tests. Ordering: shared scaffolding first, then by resource area; within an area, foundation service methods land before the wrapper that needs them. No flag → no removal phase.

---

### Phase 0 — Shared mutation scaffolding

**Goal**: Ship value: none on its own. Provide the reusable DI deps getter + org/init helper that every wrapper phase consumes, so each later phase is a thin field addition.

Changes:
1. [public_api/mutations.py](../public_api/mutations.py): add a `CalendarMutationDependencies` dataclass + `get_calendar_mutation_dependencies()` (`@inject`, `Provide["calendar_service"]`, `Provide["calendar_group_service"]`), mirroring the existing `get_calendar_group_mutation_dependencies`.
2. Add a module-level helper `_get_org_and_init_calendar_service(info)` returning `(calendar_service, org)` initialized via `initialize_without_provider(user_or_token=request.public_api_system_user, organization=org)`; raise/return-error consistently with existing resolvers.
3. No new fields registered yet.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) — `get_calendar_mutation_dependencies` raises `GraphQLError` when the provider is missing; helper raises on missing org.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Pattern copy of existing deps getter, no business logic.

**Reusable skills**: none.

Acceptance: `get_calendar_mutation_dependencies()` returns the facade + group service from the DI container, and the schema still builds (`python manage.py graphql_schema`/existing schema test passes) with no new fields.

---

> **⛔ Phases 1a / 1b / 1c DROPPED (2026-06-18).** Single-calendar event create/reschedule/cancel are not exposed via the Public API — `CalendarEventService` deliberately blocks `SystemUser` callers (see Goals → Non-goals). Phase 0 scaffolding (PR #96) is retained; later phases (resource calendars, availability, bundles) consume it. The phase bodies below are kept for the record only.

### Phase 1a — `createCalendarEvent` mutation  — DROPPED

**Goal**: External integrators can create a single-calendar event via the Public API.

Changes:
1. [public_api/constants.py](../public_api/constants.py): add `CREATE_CALENDAR_EVENT`.
2. [public_api/permissions.py](../public_api/permissions.py): add `"createCalendarEvent": PublicAPIResources.CREATE_CALENDAR_EVENT` to `FIELD_TO_RESOURCE_MAPPING`.
3. [public_api/mutations.py](../public_api/mutations.py): `CreateCalendarEventInput` (+ nested attendance/external-attendance/resource-allocation inputs — reuse the ones already defined for group events where possible) → build `CalendarEventInputData` (note `resource_allocations` use `resource_id`) → `calendar_service.create_event(calendar_id, event_data)`. Return `CreateCalendarEventResult { success, errorMessage, event }`.

Spec use-case: Create Appointment Modal → `createCalendarEvent`.

Tests:
- **Integration**: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py) — happy path creates event; unauthorized token (no `CREATE_CALENDAR_EVENT` grant) is denied; cross-org calendar id rejected; service `ValueError` → `success=False`.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Nested input mapping + recurrence/attendees plumbing.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A token granted `CREATE_CALENDAR_EVENT` creates an event on an org calendar; a token without it gets a permission error.

---

### Phase 1b — `rescheduleCalendarEvent` mutation

**Goal**: Move an existing event's start/end via the Public API.

Changes:
1. constants: add `RESCHEDULE_CALENDAR_EVENT`; permissions mapping entry.
2. mutations: `RescheduleCalendarEventInput { organizationId, calendarId, eventId, startTime, endTime, timezone }` → load current event data, apply new times into `CalendarEventInputData` → `calendar_service.update_event(calendar_id, event_id, event_data)`. Return `{ success, errorMessage, event }`.

Spec use-case: Reschedule / Cancel Modal → `rescheduleCalendarEvent`.

Tests:
- **Integration**: happy path updates times; missing event → `success=False`; cross-org event rejected; permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (iterate) / `gpt-5-mini` / `gemini-2.5-flash`. Reuses 1a plumbing.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token reschedules an event; the stored event reflects new start/end.

---

### Phase 1c — `cancelEvent` mutation

**Goal**: Cancel/delete an event (optionally the series) via the Public API.

Changes:
1. constants: add `CANCEL_EVENT`; permissions mapping entry.
2. mutations: `CancelEventInput { organizationId, calendarId, eventId, deleteSeries }` → `calendar_service.delete_event(calendar_id, event_id, delete_series)`. Return `{ success, errorMessage }`.

Spec use-case: Reschedule / Cancel Modal → `cancelEvent`.

Tests:
- **Integration**: deletes single occurrence; `deleteSeries=true` deletes series; missing/cross-org event → `success=False`; permission denied without grant.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Thin delegate, boolean flag.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token cancels an event; the event no longer appears in `calendarEvents`.

---

### Phase 2a — `createResourceCalendar` mutation

**Goal**: Admin integrators can create a manual resource (room/equipment) calendar.

Changes:
1. constants: add `CREATE_RESOURCE_CALENDAR`; permissions mapping entry.
2. mutations: `CreateResourceCalendarInput { organizationId, name, description, capacity, manageAvailableWindows }` → `calendar_service.create_resource_calendar(name, description, capacity, manage_available_windows)`. Return `{ success, errorMessage, calendar }`.

Spec use-case: Location Page → `createResourceCalendar`.

Tests:
- **Integration**: creates a resource calendar scoped to org; permission denied without grant; resulting calendar has `calendar_type=resource`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token creates a resource calendar returned by `calendars(calendarType: "resource")`.

---

### Phase 2b — `disableResourceCalendar` mutation + `disable_resource_calendar` service method

**Goal**: Admin integrators can disable a resource calendar.

Changes:
1. [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py): add `disable_resource_calendar(calendar_id) -> Calendar` — org-scoped fetch, validate it is a resource calendar, set `visibility = INACTIVE`, save. (Small, testable in the service.)
2. constants: add `DISABLE_RESOURCE_CALENDAR`; permissions mapping entry.
3. mutations: `DisableResourceCalendarInput { organizationId, calendarId }` → service call. Return `{ success, errorMessage }`.

Spec use-case: Location Page → `disableResourceCalendar`.

Tests:
- **Unit**: service test — sets `visibility=INACTIVE`; rejects cross-org id; rejects non-resource calendar.
- **Integration**: mutation happy path + permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. One service method + thin wrapper.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token disables a resource calendar; it drops out of `calendars().only_listed()`.

---

### Phase 2c — `importResourceCalendars` mutation

**Goal**: Admin integrators can trigger a Google Workspace resource import for the org.

Changes:
1. constants: add `IMPORT_RESOURCE_CALENDARS`; permissions mapping entry.
2. mutations: `ImportResourceCalendarsInput { organizationId, startTime, endTime }` → init `calendar_service` → `calendar_sync_service.request_organization_calendar_resources_import(start_time, end_time)` (accessed via the facade's sync sub-service). Return `{ success, errorMessage }` (async request — no payload).

Spec use-case: Location Page → `importResourceCalendars` (optional in doc; included).

Tests:
- **Integration**: enqueues import (assert the request method is invoked / workflow row created, mock external); permission denied without grant; missing org → error.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Verify how the facade exposes the sync sub-service during implementation.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token triggers `request_organization_calendar_resources_import` with the supplied window.

---

### Phase 3a — `createAvailabilityWindow` mutation

**Goal**: Create a single (optionally recurring) available time on a calendar.

Changes:
1. constants: add `CREATE_AVAILABILITY_WINDOW`; permissions mapping entry.
2. mutations: `CreateAvailableTimeInput { organizationId, calendarId, startTime, endTime, timezone, rruleString }` → `calendar_service.create_available_time(calendar, start_time, end_time, timezone, rrule_string)`. Return `{ success, errorMessage, availableTime }`. Note service raises if `calendar.manage_available_windows` is false → map to `success=False`.

Spec use-case: Provider Availability → `createAvailabilityWindow`.

Tests:
- **Integration**: creates window; non-managing calendar → `success=False`; permission denied without grant; cross-org calendar rejected.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token creates an available time returned by `availableTimes`.

---

### Phase 3b — `updateAvailabilityWindow` mutation

**Goal**: Update one available time via the existing batch path.

Changes:
1. constants: add `UPDATE_AVAILABILITY_WINDOW`; permissions mapping entry.
2. mutations: `UpdateAvailableTimeInput { organizationId, calendarId, availableTimeId, startTime, endTime, timezone, rruleString }` → `calendar_service.batch_modify_available_times(calendar, [{"action": "update", "id": ..., ...}])` (single-op). Return `{ success, errorMessage, availableTime }` (the matching row from the returned list).

Spec use-case: Provider Availability → `updateAvailabilityWindow`.

Tests:
- **Integration**: updates fields; missing id → `success=False` (service raises `ValueError`); permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Note the `action` key (not `op`).

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token updates an available time's start/end/rrule.

---

### Phase 3c — `deleteAvailabilityWindow` mutation

**Goal**: Delete one available time via the batch path.

Changes:
1. constants: add `DELETE_AVAILABILITY_WINDOW`; permissions mapping entry.
2. mutations: `DeleteAvailableTimeInput { organizationId, calendarId, availableTimeId }` → `batch_modify_available_times(calendar, [{"action": "delete", "id": ...}])`. Return `{ success, errorMessage }`. (Note: the v2 doc's `deleteSeries` arg has no backing — single-row delete only; omit `deleteSeries` and document the limitation.)

Spec use-case: Provider Availability → `deleteAvailabilityWindow`.

Tests:
- **Integration**: deletes row; missing id → `success=False`; permission denied without grant.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token deletes an available time; it no longer appears in `availableTimes`.

---

### Phase 3d — `batchUpdateAvailabilityWindows` mutation

**Goal**: Apply an atomic create/update/delete batch of available times.

Changes:
1. constants: add `BATCH_UPDATE_AVAILABILITY_WINDOWS`; permissions mapping entry.
2. mutations: `BatchAvailabilityInput { organizationId, calendarId, operations: [{ action, availableTimeId, startTime, endTime, timezone, rruleString }] }` → translate each op to the service dict shape (map `availableTimeId`→`id`, validate `action ∈ {create,update,delete}`) → `batch_modify_available_times`. Return `{ success, errorMessage, availableTimes }`. Whole batch rolls back on any failure (service is row-atomic in one transaction).

Spec use-case: Provider Availability → `batchUpdateAvailabilityWindows`.

Tests:
- **Integration**: mixed batch applies; one bad id rolls the whole batch back (assert no partial writes); invalid `action` rejected; permission denied without grant.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Atomic-batch edge cases.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token applies a create+update+delete batch atomically; a failing op leaves state unchanged.

---

### Phase 3e — `createBlockedTime` mutation

**Goal**: Create a single (optionally recurring) blocked time.

Changes:
1. constants: add `CREATE_BLOCKED_TIME`; permissions mapping entry.
2. mutations: `CreateBlockedTimeInput { organizationId, calendarId, startTime, endTime, timezone, reason, rruleString }` → `calendar_service.create_blocked_time(calendar, start_time, end_time, timezone, reason, rrule_string)`. Return `{ success, errorMessage, blockedTime }`.

Spec use-case: Provider Availability → `createBlockedTime`.

Tests:
- **Integration**: creates blocked time; permission denied without grant; cross-org calendar rejected.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token creates a blocked time returned by `blockedTimes`.

---

### Phase 3f — `updateBlockedTime` mutation + `update_blocked_time` service method

**Goal**: Update one blocked time (true gap — no service method today).

Changes:
1. [calendar_integration/services/availability_service.py](../calendar_integration/services/availability_service.py): add `update_blocked_time(calendar, blocked_time_id, start_time=None, end_time=None, timezone=None, reason=None, rrule_string=None) -> BlockedTime` — org-scoped + calendar-scoped fetch (mirror the `scoped` filter in `batch_modify_available_times`), apply provided fields, save; raise `ValueError` on missing id. Add facade delegate on `CalendarService`.
2. constants: add `UPDATE_BLOCKED_TIME`; permissions mapping entry.
3. mutations: `UpdateBlockedTimeInput { organizationId, calendarId, blockedTimeId, startTime, endTime, timezone, reason, rruleString }` → service call. Return `{ success, errorMessage, blockedTime }`.

Spec use-case: Provider Availability → `updateBlockedTime`.

Tests:
- **Unit**: service test — updates fields, org/calendar scoping, missing id raises `ValueError`.
- **Integration**: mutation happy path; missing id → `success=False`; permission denied without grant.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New service method + scoping + wrapper.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token updates a blocked time's fields; cross-calendar/cross-org ids rejected.

---

### Phase 3g — `deleteBlockedTime` mutation + `delete_blocked_time` service method

**Goal**: Delete one blocked time (true gap — no service method today).

Changes:
1. [calendar_integration/services/availability_service.py](../calendar_integration/services/availability_service.py): add `delete_blocked_time(calendar, blocked_time_id, delete_series=False) -> None` — org/calendar-scoped delete; `delete_series` deletes the recurring parent + children consistent with how blocked-time recurrence is modeled (confirm during impl); raise `ValueError` on missing id. Add facade delegate.
2. constants: add `DELETE_BLOCKED_TIME`; permissions mapping entry.
3. mutations: `DeleteBlockedTimeInput { organizationId, calendarId, blockedTimeId, deleteSeries }` → service call. Return `{ success, errorMessage }`.

Spec use-case: Provider Availability → `deleteBlockedTime`.

Tests:
- **Unit**: service test — deletes single + series, org/calendar scoping, missing id raises.
- **Integration**: mutation happy path; permission denied without grant.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Series-delete semantics need care.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token deletes a blocked time (and series when requested); it no longer appears in `blockedTimes`.

---

### Phase 4a — `CalendarBundleGraphQLType` + `calendarBundles` query

**Goal**: Bundles become a first-class Public API read surface.

Changes:
1. [calendar_integration/graphql.py](../calendar_integration/graphql.py): add `CalendarBundleGraphQLType` over `Calendar` (filtered to `calendar_type=BUNDLE`), exposing `id, name, description, children { id name }` (children via `ChildrenCalendarRelationship`). No `isPrivate`/`owners` (non-goals).
2. constants: add `CALENDAR_BUNDLE` (new resource category); permissions mapping entries for `calendarBundles` (and reuse for the bundle mutations in 4b–4d, each still getting its own value — see Open Questions if a single category is preferred).
3. [public_api/queries.py](../public_api/queries.py): `calendarBundles(offset, limit)` → `Calendar.objects.filter_by_organization(org.id).filter(calendar_type=BUNDLE)`, paginated via `_slice_qs`. Address child prefetch to avoid N+1.

Spec use-case: Appointment Types & Calendar Groups & Bundles → List calendar bundles.

Tests:
- **Integration**: lists bundles for org only (cross-org isolation); children resolve; pagination bounds; permission denied without `CALENDAR_BUNDLE` grant.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New type + query + prefetch.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token lists the org's bundle calendars with their children; other orgs' bundles never appear.

---

### Phase 4b — `createCalendarBundle` mutation

**Goal**: Create a bundle calendar from child calendars.

Changes:
1. constants: add `CREATE_CALENDAR_BUNDLE`; permissions mapping entry.
2. mutations: `CreateCalendarBundleInput { organizationId, name, description, childrenIds, primaryCalendarId }` → resolve child Calendars (org-scoped) + primary → `calendar_service.create_bundle_calendar(name, description, child_calendars, primary_calendar)`. Return `{ success, errorMessage, bundle }`. (No `isPrivate` — non-goal.)

Spec use-case: Appointment Types & Calendar Groups & Bundles → `createCalendarBundle`.

Tests:
- **Integration**: creates bundle with children; cross-org child id rejected; permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token creates a bundle returned by `calendarBundles`.

---

### Phase 4c — `updateCalendarBundle` mutation

**Goal**: Update a bundle's children/primary.

Changes:
1. constants: add `UPDATE_CALENDAR_BUNDLE`; permissions mapping entry.
2. mutations: `UpdateCalendarBundleInput { organizationId, bundleId, name, description, childrenIds, primaryCalendarId }` → org-scoped fetch of the bundle Calendar + resolve children → `calendar_service.update_bundle_calendar(bundle_calendar, child_calendars, primary_calendar)`. Return `{ success, errorMessage, bundle }`. (`name`/`description` update applied on the bundle Calendar if the service does not — confirm during impl.)

Spec use-case: Appointment Types & Calendar Groups & Bundles → `updateCalendarBundle`.

Tests:
- **Integration**: updates children set; missing/cross-org bundle → `success=False`; permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token replaces a bundle's children; `calendarBundles` reflects the change.

---

### Phase 4d — `disableCalendarBundle` mutation + `disable_bundle_calendar` service method

**Goal**: Disable a bundle calendar.

Changes:
1. [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py): add `disable_bundle_calendar(bundle_id) -> Calendar` — org-scoped fetch, validate `calendar_type=BUNDLE`, set `visibility=INACTIVE`, save.
2. constants: add `DISABLE_CALENDAR_BUNDLE`; permissions mapping entry.
3. mutations: `DisableCalendarBundleInput { organizationId, bundleId }` → service call. Return `{ success, errorMessage }`.

Spec use-case: Appointment Types & Calendar Groups & Bundles → `disableCalendarBundle`.

Tests:
- **Unit**: service test — sets `visibility=INACTIVE`; rejects non-bundle / cross-org id.
- **Integration**: mutation happy path; permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token disables a bundle; it drops out of the listed bundles.

---

### Phase 5a — `reschedule_grouped_event` service method (foundation)

**Goal**: Ship value: none on its own. Add the missing `CalendarGroupService` method that moves a grouped event's start/end (and optionally re-selects slot calendars), coordinating the change across the grouped event's child events. **True gap** — no reschedule path exists today; `create_grouped_event` is the only write.

Changes:
1. [calendar_integration/services/calendar_group_service.py](../calendar_integration/services/calendar_group_service.py): add `reschedule_grouped_event(event_id, start_time, end_time, timezone, slot_selections=None) -> CalendarEvent`. Design notes for the implementer: a grouped event is a `CalendarEvent` spanning multiple slot-selected child calendars; rescheduling must (a) validate the new window against availability for the selected calendars (reuse `check_group_availability` / `find_bookable_slots`), (b) update the parent grouped event times, (c) update/re-create the per-slot child events consistently, in one transaction. Confirm the exact child-event model relationship before coding. Compare with `CalendarBundleService.update_bundle_event` (lines ~409–475) as a precedent for multi-calendar event updates.
2. Raise typed errors (`CalendarGroupError`) for invalid group/event/slot states.

Spec use-case: shared scaffolding for `rescheduleCalendarGroupEvent` — no GraphQL field yet.

Tests:
- **Unit**: service tests — reschedule keeps group invariants (required_count per slot still satisfied); rejects unavailable new window; transactional rollback on partial failure; cross-org event rejected.

**Suggested AI model**: Tier 4 — `claude-opus-4-7[1m]` / `gpt-5` (extended thinking) / `gemini-3-pro`. Architectural multi-calendar transactional logic with availability validation.

**Reusable skills**: `write-tests`.

Acceptance: `reschedule_grouped_event` moves a grouped event and all its child events atomically, validating availability; full service test suite green.

---

### Phase 5b — `rescheduleCalendarGroupEvent` mutation

**Goal**: Reschedule a grouped event via the Public API.

Changes:
1. constants: add `RESCHEDULE_CALENDAR_GROUP_EVENT`; permissions mapping entry.
2. mutations (in `CalendarGroupMutations` or alongside): `RescheduleGroupEventInput { organizationId, groupId, eventId, startTime, endTime, timezone, slotSelections: [{ slotId, calendarIds }] }` → init `calendar_group_service` → `reschedule_grouped_event(...)`. Return `{ success, errorMessage, event }`.

Spec use-case: Reschedule / Cancel Modal → `rescheduleCalendarGroupEvent`.

Tests:
- **Integration**: reschedules a grouped event; unavailable window → `success=False`; missing event/group → `success=False`; permission denied without grant.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Thin wrapper over 5a.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: A granted token reschedules a grouped event; the change is reflected across its child events.

---

## 6. Risk & Rollout Notes

- **No feature flag**: brand-new GraphQL fields, no existing caller touched; per-token `PublicAPIResources` grants are the access gate. A token sees a new field only after an admin grants the matching resource (REST `POST /public-api-tokens/` with `available_resources`).
- **No migrations**: no schema/locks/partition concerns. All new server work is Python service methods.
- **Permission-matrix growth**: ~17 new `PublicAPIResources` values. Document them so admins know what to grant; ensure the REST token-creation surface accepts the new values (it reads `PublicAPIResources`, so additions are automatic — verify in Phase 0/2a).
- **Org isolation**: every resolver must `filter_by_organization(org.id)` before touching a row; tests assert cross-org ids are rejected (recurring bug source).
- **Batch atomicity**: `batchUpdateAvailabilityWindows` relies on the service's single-transaction row-atomic guarantee; test asserts rollback on a bad op.
- **Group reschedule (5a) is the only real risk**: multi-calendar transactional update + availability validation. Land + soak its service tests before wiring 5b. If 5a proves larger than ~300 LoC, split into `5a-i` (times-only reschedule) and `5a-ii` (slot re-selection).
- **Rollback**: any phase reverts cleanly by removing its field + resource value + mapping entry (and service method for gap phases); no data migration to undo.

## 7. Open Questions

| Question | Recommended default |
|---|---|
| Should the three bundle mutations + `calendarBundles` query each get a distinct `PublicAPIResources` value, or share one `CALENDAR_BUNDLE` category? | Per the Step 0 decision, **distinct value per field** (`CALENDAR_BUNDLE` for the read, `CREATE/UPDATE/DISABLE_CALENDAR_BUNDLE` for writes). Revisit only if the token-grant UX becomes unwieldy. |
| `deleteAvailabilityWindow` / `deleteBlockedTime` `deleteSeries` semantics for recurring rows — what exactly does "series" mean against the recurring-exception model? | Confirm during Phase 3c/3g against the recurrence model; default to single-row delete + document the limitation if series delete is non-trivial, and split a follow-up phase. |
| Does `update_bundle_calendar` persist `name`/`description`, or only children/primary? | Confirm in Phase 4c; if not, set them on the bundle Calendar in the resolver. |
| Exact child-event relationship for grouped events (for 5a). | Resolve during Phase 5a design before coding; precedent in `CalendarBundleService.update_bundle_event`. |

## 8. Touch List

**Phase 0**: edit [public_api/mutations.py](../public_api/mutations.py); test [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py).

**Phase 1a–1c** (events): edit [public_api/constants.py](../public_api/constants.py), [public_api/permissions.py](../public_api/permissions.py), [public_api/mutations.py](../public_api/mutations.py); tests in [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py).

**Phase 2a–2c** (resource calendars): edit constants, permissions, mutations; `@calendar_integration/services/calendar_service.py` (`disable_resource_calendar`, Phase 2b); tests in test_mutations.py + a calendar-service test module for the new method.

**Phase 3a–3g** (availability/blocked): edit constants, permissions, mutations; `@calendar_integration/services/availability_service.py` + facade delegates in `@calendar_integration/services/calendar_service.py` (`update_blocked_time` Phase 3f, `delete_blocked_time` Phase 3g); tests in test_mutations.py + availability-service test module.

**Phase 4a–4d** (bundles): new `CalendarBundleGraphQLType` in `@calendar_integration/graphql.py`; edit [public_api/queries.py](../public_api/queries.py) (`calendarBundles`), constants, permissions, mutations; `@calendar_integration/services/calendar_service.py` (`disable_bundle_calendar`, Phase 4d); tests in [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) (4a) + test_mutations.py (4b–4d).

**Phase 5a–5b** (group reschedule): `@calendar_integration/services/calendar_group_service.py` (`reschedule_grouped_event`, Phase 5a); edit constants, permissions, mutations (5b); tests in the calendar-group-service test module (5a) + test_mutations.py (5b).

**Schema**: no edits to [public_api/schema.py](../public_api/schema.py) needed — it imports `Query`/`Mutation` whole.
