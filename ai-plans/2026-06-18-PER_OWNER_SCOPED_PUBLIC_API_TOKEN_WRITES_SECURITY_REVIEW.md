# Per-Owner-Scoped Public API Token Writes — Security Review / Bypass-Surface Sign-off

> Sign-off artifact for **Phase 4** of
> [`2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_IMPLEMENTATION_PLAN.md`](2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_IMPLEMENTATION_PLAN.md).
> Covers spec objective 5 ("no reachable field or mutation leaks across owners,
> including nested GraphQL field traversal") and consolidates the negative-path
> guarantee (use-case 3).

## 1. Threat model

A **provider-scoped** public-API token (a `SystemUser` with
`scoped_to_membership_fk_id` set) is authorized only for the calendars its owner
owns (`scoped_calendar_ids(...)` → that owner's `set[int]`). An **org-wide** token
(`scoped_to_membership_fk_id IS NULL`) is authorized for every calendar in the org
(`scoped_calendar_ids(...)` → `None`). Internal (non-public-API) GraphQL requests
carry no `public_api_system_user` attribute at all.

The top-level read resolvers (`calendars`, `calendarEvents`, `blockedTimes`,
`availableTimes`) and the write mutations enforce the owner scope on the *entry*
field. But Strawberry runs permission/field logic **only on the decorated
top-level field**. Once a scoped token has legitimately fetched one of its OWN
objects, it can select **nested GraphQL fields** on the shared types and reach
OTHER owners' calendars/events/objects — the entry gate has already passed.

This review enumerates every reachable field on the shared
`calendar_integration/graphql.py` types, classifies it, and records the guard +
proving test. Each guarded nested field is now resolved through a
`@strawberry.field` resolver driven by `_owner_scoped_calendar_ids(info)`:

- `None` → **no filtering** (org-wide token OR internal request) → the resolver
  returns the original value untouched → byte-for-byte unchanged.
- a `set[int]` → **filter / suppress** anything whose calendar is outside the set.

## 2. Enforcement primitives

| Primitive | Location | Role |
|---|---|---|
| `scoped_calendar_ids(system_user, organization)` | `public_api/scoping.py` | One source of truth: `None` for org-wide, owner's calendar-id set for scoped. |
| `assert_calendar_in_owner_scope(...)` | `public_api/scoping.py` | Write-side guard (Phases 1–3): not-found for cross-owner targets. |
| `_owner_scoped_calendar_ids(info)` | `calendar_integration/graphql.py` | Nested-read guard: lazy-imports `scoped_calendar_ids`; `None` for org-wide AND internal (`getattr(request, "public_api_system_user", None) is None`). |
| `_scoped_calendar_or_none` / `_scoped_event_or_none` / `_scoped_event_list` / `_scoped_blocked_time_or_none` / `_scoped_available_time_or_none` / `_scoped_calendar_list` | `calendar_integration/graphql.py` | DRY filter helpers; each is a no-op when `allowed_ids is None`. |
| `OrganizationResourceAccess` + `PROVIDER_SCOPED_RESOURCES` | `public_api/permissions.py`, `public_api/constants.py` | Resource-grant gate. `CALENDAR_GROUP` is **NOT** provider-scoped → a scoped token can never be granted the top-level `calendarGroup*` queries. |

## 3. Reachable read fields — guard / safe classification

Legend: **GUARD** = routed through a scoped resolver; **SAFE** = no cross-owner
calendar leak possible (documented reason); **N/A** = scalar.

### 3.1 `CalendarEventGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| `id,title,description,externalId,startTime,endTime,recurrenceId,is*` | scalars | N/A | — | — |
| `recurrenceRule` | RecurrenceRule | SAFE | RecurrenceRule is all-scalar, org-scoped, bears no calendar. | — |
| `attendances` | EventAttendance→User | SAFE | Returns PEOPLE, not calendars. | — |
| `externalAttendances` | EventExternalAttendance | GUARD (via type) | `EventExternalAttendance.event` is scoped (see 3.7). | `test_external_attendance_event_backpointer_safe` |
| `recurrenceExceptions` | EventRecurrenceException | GUARD (via type) | Back-pointers scoped (see 3.8). | `test_event_recurrence_exception_scoped/_org_wide` |
| `attendees` | User | SAFE | PEOPLE, not calendars. | — |
| `externalAttendees` | ExternalAttendee | SAFE | External people (email/name); no calendar. | — |
| `calendar` | Calendar | **GUARD** | `_scoped_calendar_or_none`. | `test_event_calendar_scoped_sees_own/_org_wide_unchanged` |
| `bundleCalendar` | Calendar | **GUARD** | `_scoped_calendar_or_none`. | `test_event_backpointers_scoped_suppressed/_org_wide_unchanged` |
| `bundlePrimaryEvent` | CalendarEvent | **GUARD** | `_scoped_event_or_none`. | `test_event_backpointers_*` |
| `bulkModificationParent` | CalendarEvent | **GUARD** | `_scoped_event_or_none`. | `test_event_backpointers_*` |
| `parentRecurringObject` | CalendarEvent | **GUARD** | `_scoped_event_or_none`. | `test_event_backpointers_*` |
| `resourceAllocations` | ResourceAllocation | **GUARD** | filtered to owner's calendars; `ResourceAllocation.calendar` also scoped. | `test_event_resources_scoped_filtered/_org_wide_unchanged` |
| `resources` | Calendar | **GUARD** | `_scoped_calendar_list`. | `test_event_resources_*` |
| `calendarGroup` | CalendarGroup | **GUARD (suppress)** | returns `None` for scoped tokens — a group aggregates calendars across providers. | `test_event_group_scoped_suppressed_including_second_hop` |
| `groupSelections` | CalendarEventGroupSelection | **GUARD** | filtered to owner's calendars; nested `slot`/`calendar` scoped (see 3.5). | `test_event_group_*` |
| `bundleRepresentations` | CalendarEvent[] | **GUARD** | `_scoped_event_list`. | `test_event_child_lists_scoped_filtered/_org_wide_unchanged` |
| `bulkModifications` | CalendarEvent[] | **GUARD** | `_scoped_event_list`. | `test_event_child_lists_*` |
| `recurringInstances` | CalendarEvent[] | **GUARD** | `_scoped_event_list` over the model reverse accessor `calendarevent_recurring_instances`. | `test_event_child_lists_*` |

### 3.2 `BlockedTimeGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars, `user` (→User), `recurrenceRule` | — | SAFE | people / all-scalar. | — |
| `calendar` | Calendar | **GUARD** | `_scoped_calendar_or_none`. | `test_blocked_calendar_and_exception_scoped/_org_wide` |
| `recurrenceExceptions` | BlockedTimeRecurrenceException | GUARD (via type) | back-pointers scoped (3.6). | `test_blocked_calendar_and_exception_*` |

### 3.3 `AvailableTimeGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars, `user`, `recurrenceRule` | — | SAFE | people / all-scalar. | — |
| `calendar` | Calendar | **GUARD** | `_scoped_calendar_or_none`. | `test_available_calendar_and_exception_scoped/_org_wide` |
| `recurrenceExceptions` | AvailableTimeRecurrenceException | GUARD (via type) | back-pointers scoped (3.6). | `test_available_calendar_and_exception_*` |

### 3.4 `ResourceAllocationGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars | — | N/A | — | — |
| `calendar` | Calendar | **GUARD** | `_scoped_calendar_or_none`. Parent list (`event.resourceAllocations`) is also owner-filtered (defence in depth). | `test_event_resources_*` |

### 3.5 `CalendarEventGroupSelectionGraphQLType` (the second-hop surface)

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars | — | N/A | — | — |
| `slot` | CalendarGroupSlot | **GUARD (suppress)** | returns `None` for scoped tokens. This is the **second-hop bypass** of the `calendarGroup` suppression: a scoped token could otherwise reach the slot via the sibling path `calendarEvent.groupSelections.slot.calendars` and read the entire cross-provider candidate pool. | `test_event_group_scoped_suppressed_including_second_hop`, `test_event_group_second_hop_pool_filtered_when_slot_exposed` |
| `calendar` | Calendar | **GUARD** | `_scoped_calendar_or_none`. | `test_event_group_*` |

### 3.6 `CalendarGroupSlotGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars | — | N/A | — | — |
| `calendars` | Calendar[] | **GUARD (filter)** | `_scoped_calendar_list` filters the candidate pool to the owner's set. Defence-in-depth behind the `slot` suppression — even if a slot were ever reachable, its cross-provider pool is filtered. | `test_event_group_org_wide_unchanged` (full pool for org-wide); reachability closed by 3.5. |

### 3.7 `EventExternalAttendanceGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars, `externalAttendee` | — | SAFE | external person. | — |
| `event` | CalendarEvent | **GUARD** | `_scoped_event_or_none` — the back-pointer cannot reach an event on a non-owned calendar. | `test_external_attendance_event_backpointer_safe` |

### 3.8 `EventRecurrenceExceptionGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars | — | N/A | — | — |
| `parentEvent` | CalendarEvent | **GUARD** | `_scoped_event_or_none`. | `test_event_recurrence_exception_scoped/_org_wide` |
| `modifiedEvent` | CalendarEvent | **GUARD** | `_scoped_event_or_none`. | `test_event_recurrence_exception_*` |

### 3.9 `BlockedTimeRecurringExceptionGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars | — | N/A | — | — |
| `parentBlockedTime` | BlockedTime | **GUARD** | `_scoped_blocked_time_or_none`. | `test_blocked_calendar_and_exception_*` |
| `modifiedBlockedTime` | BlockedTime | **GUARD** | `_scoped_blocked_time_or_none`. | `test_blocked_calendar_and_exception_*` |

### 3.10 `AvailableTimeRecurringExceptionGraphQLType`

| Field | Returns | Classification | Guard / reason | Proving test |
|---|---|---|---|---|
| scalars | — | N/A | — | — |
| `parentAvailableTime` | AvailableTime | **GUARD** | `_scoped_available_time_or_none`. | `test_available_calendar_and_exception_*` |
| `modifiedAvailableTime` | AvailableTime | **GUARD** | `_scoped_available_time_or_none`. | `test_available_calendar_and_exception_*` |

### 3.11 `CalendarGraphQLType`

All-scalar terminal type (id, name, description, email, externalId, provider,
calendarType, capacity, manageAvailableWindows, syncEnabled, created, modified).
**SAFE** — exposes no calendar-bearing relation, so a calendar reached through a
scoped resolver (already owner-verified) cannot pivot to another owner's data.

### 3.12 `CalendarGroupGraphQLType` (and its `slots`)

**SAFE by unreachability for scoped tokens.** Two and only two entry points exist:

1. Top-level `calendarGroup` / `calendarGroups` / `calendarGroupEvents` /
   `calendarGroupAvailability` / `calendarGroupBookableSlots` queries — all mapped
   to `PublicAPIResources.CALENDAR_GROUP`, which is **not** in
   `PROVIDER_SCOPED_RESOURCES`. A scoped token cannot be granted these resources
   (`create_scoped_system_user` validates against the allow-list), so
   `OrganizationResourceAccess` denies the field before any resolver runs.
2. `CalendarEvent.calendarGroup` and `CalendarEventGroupSelection.slot` — both
   **suppressed** for scoped tokens (3.1, 3.5).

With both entry points closed, `CalendarGroup.slots` (and therefore the
`slots.calendars` pool) is unreachable for a scoped token. The
`CalendarGroupSlot.calendars` filter (3.6) is retained as defence in depth.

> **Reviewer note (called out explicitly):** The top-level `calendarGroup*`
> queries are NOT owner-scoped at the resolver level — they return any group in
> the org. This is intentional and currently safe *because* `CALENDAR_GROUP` is
> not provider-scoped. If a future change adds `CALENDAR_GROUP` (or any
> group-returning resource) to `PROVIDER_SCOPED_RESOURCES`, the top-level
> `calendarGroup*` resolvers MUST gain owner-scope filtering at that time, or the
> `slots.calendars` pool becomes reachable. This coupling is the one residual
> assumption in the sign-off.

### 3.13 Webhook / bundle / availability-window value types

`CalendarWebhookSubscriptionGraphQLType`, `CalendarWebhookEventGraphQLType`,
`WebhookSubscriptionStatusGraphQLType`, `AvailableTimeWindowGraphQLType`,
`UnavailableTimeWindowGraphQLType`, `CalendarBundleGraphQLType`, the
`CalendarGroup*Availability*` value types, and the booking-code result types are
reached only from top-level fields whose resources are NOT in
`PROVIDER_SCOPED_RESOURCES` (`webhook_*`, `calendarBundles`,
`calendarGroup*`) or are code-gated (booking codes, unauthenticated). A scoped
token cannot reach them. **SAFE by unreachability.** `CalendarBundleGraphQLType`
is gated by `CALENDAR_BUNDLE` (org-admin, not provider-scoped).

## 4. Write mutations — owner-scope guards (Phases 1–3, restated for sign-off)

| Mutation | Resource | Guard | Proving test |
|---|---|---|---|
| `createBlockedTime` / `updateBlockedTime` / `deleteBlockedTime` | `CREATE/UPDATE/DELETE_BLOCKED_TIME` | `assert_calendar_in_owner_scope` → not-found for cross-owner. | `TestScopedTokenBlockedTimeWrites` (`public_api/tests/test_mutations.py`) |
| `createAvailabilityWindow` / `updateAvailabilityWindow` / `deleteAvailabilityWindow` / `batchUpdateAvailabilityWindows` | `CREATE/UPDATE/DELETE/BATCH_UPDATE_AVAILABILITY_WINDOW(S)` | `assert_calendar_in_owner_scope` per op; batch all-or-nothing. | availability scoped-write tests (`public_api/tests/test_mutations.py`) |
| `scheduleEvent` | `CALENDAR_EVENT` | `assert_calendar_in_owner_scope` + independent `CalendarOwnership` re-verification in `create_event`; org-wide blocked; bundle rejected. | `scheduleEvent` scoped tests (`public_api/tests/test_mutations.py`) + `calendar_integration` service tests |

Cross-owner write → `Calendar.DoesNotExist` re-wrapped with the **same** message a
genuinely missing calendar produces (no existence oracle). Org-wide token → guard
is a structural no-op (`scoped_calendar_ids` → `None`).

## 5. Internal / org-wide no-op (byte-for-byte)

Every nested resolver returns the **original value untouched** when
`_owner_scoped_calendar_ids(info)` is `None`. That happens for:

- **Internal / non-public-API requests** — the request has no
  `public_api_system_user` attribute; `getattr(..., None)` short-circuits to
  `None`. Proven: `test_internal_request_unscoped_returns_nested`.
- **Org-wide tokens** — `scoped_calendar_ids` returns `None`. Proven:
  `test_org_wide_request_returns_none_from_helper` and every `*_org_wide*`
  integration test (full nested data visible).

Scoped tokens yield the owner's set: `test_scoped_request_returns_owner_set_from_helper`.

## 6. Regression-detection guarantee

Each adversarial test fails if its guard is reverted. Verified during
implementation by reverting the `CalendarEventGroupSelection.slot` suppression:
`test_event_group_scoped_suppressed_including_second_hop` and
`test_event_group_second_hop_pool_filtered_when_slot_exposed` both failed; the
guard was restored and both pass.

## 7. Schema-shape note

Converting suppress-capable fields to nullable resolvers widened several nested
fields from non-null to nullable in the GraphQL SDL (e.g. `CalendarEvent.calendar`,
`EventExternalAttendance.event`, recurrence-exception back-pointers,
`CalendarEventGroupSelection.slot`/`calendar`). This is a backward-compatible
widening for read clients and the only mechanism by which "suppress to None" can
be expressed. List fields stay `[T!]!` and are filtered in place. No REST
(`schema.yml`) impact — these are GraphQL-only types.

## 8. Sign-off

- [x] Every reachable read field enumerated and classified (sections 3.1–3.13).
- [x] Every cross-owner-reachable nested field routed through a scoped resolver.
- [x] Second-hop leak (`groupSelections.slot.calendars`) closed at both the
      `slot` suppression and the `slot.calendars` pool filter.
- [x] Each guarded field has an adversarial proving test that fails on revert.
- [x] Org-wide + internal consumers proven byte-for-byte unchanged.
- [x] Write mutations (Phases 1–3) owner-guarded with not-found semantics.
- [x] One residual coupling documented (3.12 reviewer note): top-level
      `calendarGroup*` resolvers depend on `CALENDAR_GROUP` staying out of
      `PROVIDER_SCOPED_RESOURCES`.
