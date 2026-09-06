# API changes: CalendarGroup renamed to AppointmentType

- **Date:** 2026-09-06
- **Scope:** `claude/rename-calendargroup-appointmenttype-e192f6` vs `main` (`5ed761bd..bab58799`), PR #330
- **Audience:** Web SPA (React), Partner integrations
- **Breaking changes:** yes — every appointment-type surface is renamed, and three widely-used response payloads change field names

## Summary

The domain object previously called a **calendar group** is now an **appointment type**. It is the same object with the same behaviour — a booking template that names the roles a booking must fill, each role holding a pool of candidate calendars and a `required_count`. Nothing about how it works changed; only what everything is called.

The rename is total and mechanical: 32 REST paths, 58 REST operation IDs, 28 REST schema components, 25 GraphQL query/mutation fields, and every GraphQL type and input that mentioned it. There is **no deprecation window** — the old names are gone in the same release, so this is a coordinated cut-over, not a migration you can stage.

Three changes are easy to miss because they land on endpoints whose URL did not change: `CalendarEvent.group_selections`, `BookingPolicy.calendar_group`, and `BookingCodeCreate.calendar_group`. Read [Breaking changes](#breaking-changes) before assuming your integration is unaffected.

---

## Breaking changes

### 1. Payload fields renamed on endpoints whose URL is unchanged

These are the ones that break silently. Your requests still reach a valid endpoint; the field you read is simply absent.

| Schema | Before | After | Type | Notes |
| --- | --- | --- | --- | --- |
| `CalendarEvent` | `group_selections` | `appointment_type_selections` | `array<CalendarEventAppointmentTypeSelection>`, read-only, **required** | Present on every calendar-event read across the API |
| `CalendarEventWithManagementCodes` | `group_selections` | `appointment_type_selections` | same | |
| `PatchedCalendarEvent` | `group_selections` | `appointment_type_selections` | same | |
| `BookingPolicy` | `calendar_group` | `appointment_type` | `integer`, nullable | The policy's target |
| `PatchedBookingPolicy` | `calendar_group` | `appointment_type` | `integer`, nullable | |
| `BookingCodeCreate` | `calendar_group` | `appointment_type` | `integer`, nullable | Request body when minting a code |
| `BookingCodeCreateResult` | `calendar_group` | `appointment_type` | `integer`, read-only, nullable | Mint response |

The item type inside the selections array is also renamed: `CalendarEventGroupSelection` → `CalendarEventAppointmentTypeSelection`. Its own fields (`id`, `slot`, `calendar`, `is_in_current_roster`) are unchanged.

The same three renames apply on GraphQL — see [GraphQL type fields](#graphql-type-fields).

### 2. Every appointment-type URL moved

`/calendar-groups/…` → `/appointment-types/…`, and the nested path parameter `{group_id}` → `{appointment_type_id}`. Full mapping in [REST paths](#rest-paths). Requests to the old paths return **404**.

### 3. Every appointment-type GraphQL field, type and input renamed

`calendarGroups` → `appointmentTypes`, `createCalendarGroup` → `createAppointmentType`, and so on. Arguments move too: `groupId` / `calendarGroupId` → `appointmentTypeId`, `groupSlotId` → `appointmentTypeSlotId`. Full mapping in [GraphQL](#graphql). Queries naming the old fields fail validation with `Cannot query field …`, so the whole document is rejected — a stale field in one part of a batched query takes the rest with it.

### 4. Partner API token scopes renamed

Seven values in the `available_resources` enum changed. **Existing tokens keep their grants** — a data migration rewrites stored grants in place — but any client that *sends* these values when provisioning or updating a token must send the new ones. See [Token scopes](#token-scopes).

### 5. Billing resource key renamed

`calendar_groups` → `appointment_types` in the `resource_key` enum, which appears in plan limits, add-on purchases and usage responses. Existing rows are migrated. See [Billing resource key](#billing-resource-key).

### 6. The calendar-pool 409 conflict body changed shape

`DELETE /calendar-pools/{id}/` returns 409 when the pool is still attached. The key naming the blockers changed:

```diff
- { "detail": "...", "groups": ["Annual Physical", "Follow-up"] }
+ { "detail": "...", "appointment_types": ["Annual Physical", "Follow-up"] }
```

### 7. Human-readable error text changed

Several `detail` strings now say "appointment type" where they said "group" or "calendar group". If you match on message text rather than `error_code`, those matches break. `error_code` values are **unchanged** — matching on them is safe and is what you should do.

Examples of changed text:

- `"This code is not scoped to a calendar group."` → `"This code is not scoped to an appointment type."`
- `"This code does not permit booking on this calendar group."` → `"…on this appointment type."`
- `"This group does not accept public scheduling. …"` → `"This appointment type does not accept public scheduling. …"`
- `"This code is scoped to a calendar group. Use the group reschedule endpoint…"` → `"This code is scoped to an appointment type. Use the appointment type reschedule endpoint…"`

### Deadline

There is none, because there is no overlap period: the old names stop existing the moment this ships. Clients must land their changes in the same deploy window.

---

## What did **not** change

Worth stating, because it narrows the work:

- **No auth or permission change.** Same schemes, same required scopes, same admin/member split, same 403-vs-404 behaviour.
- **No status-code change** on any operation.
- **No `error_code` value change.** Only the human-readable `detail` text moved.
- **No pagination, filtering or sorting change.** `GET /appointment-types/` still takes `X-Organization-Id`, `limit`, `offset`, `name` — exactly as `GET /calendar-groups/` did.
- **No field type, nullability or requiredness change** anywhere. Every renamed field keeps its exact type and constraints.
- **No change to the `ruleType` values** in appointment-type-scoped rule violations (`outside_window`, `inside_block`, `over_quota` and friends). The enum class was renamed in the backend; its wire values were not.
- **No webhook payload change.**
- **205 REST paths and 158 REST schema components are untouched.**

---

## REST

### REST paths

Old path → new path. Every `{format}` suffix variant follows the same rule and is omitted here for brevity.

| Before | After |
| --- | --- |
| `/calendar-groups/` | `/appointment-types/` |
| `/calendar-groups/{id}/` | `/appointment-types/{id}/` |
| `/calendar-groups/{id}/availability/` | `/appointment-types/{id}/availability/` |
| `/calendar-groups/{id}/bookable-slots/` | `/appointment-types/{id}/bookable-slots/` |
| `/calendar-groups/{id}/booked-events/` | `/appointment-types/{id}/booked-events/` |
| `/calendar-groups/{id}/events/` | `/appointment-types/{id}/events/` |
| `/calendar-groups/{id}/stale-selections/` | `/appointment-types/{id}/stale-selections/` |
| `/calendar-groups/{group_id}/slots/{slot_id}/availability-windows/` | `/appointment-types/{appointment_type_id}/slots/{slot_id}/availability-windows/` |
| `/calendar-groups/{group_id}/slots/{slot_id}/availability-windows/{id}/` | `/appointment-types/{appointment_type_id}/slots/{slot_id}/availability-windows/{id}/` |
| `/calendar-groups/{group_id}/slots/{slot_id}/blocked-times/` | `/appointment-types/{appointment_type_id}/slots/{slot_id}/blocked-times/` |
| `/calendar-groups/{group_id}/slots/{slot_id}/blocked-times/{id}/` | `/appointment-types/{appointment_type_id}/slots/{slot_id}/blocked-times/{id}/` |
| `/calendar-groups/{group_id}/slots/{slot_id}/quota-rules/` | `/appointment-types/{appointment_type_id}/slots/{slot_id}/quota-rules/` |
| `/calendar-groups/{group_id}/slots/{slot_id}/quota-rules/{id}/` | `/appointment-types/{appointment_type_id}/slots/{slot_id}/quota-rules/{id}/` |
| `/public/booking/calendar-group-availability/` | `/public/booking/appointment-type-availability/` |
| `/public/booking/calendar-group-bookable-slots/` | `/public/booking/appointment-type-bookable-slots/` |
| `/public/booking/calendar-groups/{public_slug}/availability/` | `/public/booking/appointment-types/{public_slug}/availability/` |
| `/public/booking/calendar-groups/{public_slug}/bookable-slots/` | `/public/booking/appointment-types/{public_slug}/bookable-slots/` |
| `/public/booking/calendar-groups/{public_slug}/events/` | `/public/booking/appointment-types/{public_slug}/events/` |
| `/public/booking/group-events/reschedule/` | `/public/booking/appointment-type-events/reschedule/` |

> The last row is the only path that does not follow the plain `calendar-groups` → `appointment-types` substitution. Note the segment is `appointment-type-events`, singular, not `appointment-types-events`.

### REST operation IDs

If you generate a client from `schema.yml`, all 58 appointment-type operation IDs are renamed by one rule:

```
calendar_groups_*                      →  appointment_types_*
public_booking_calendar_group_*        →  public_booking_appointment_type_*
public_booking_calendar_groups_*       →  public_booking_appointment_types_*
public_booking_group_events_reschedule_create
                                       →  public_booking_appointment_type_events_reschedule_create
```

Examples: `calendar_groups_list` → `appointment_types_list`; `calendar_groups_slots_quota_rules_partial_update` → `appointment_types_slots_quota_rules_partial_update`.

### REST schema components

All 28 renamed 1:1:

| Before | After |
| --- | --- |
| `CalendarGroup` | `AppointmentType` |
| `PatchedCalendarGroup` | `PatchedAppointmentType` |
| `PaginatedCalendarGroupList` | `PaginatedAppointmentTypeList` |
| `CalendarGroupSlot` | `AppointmentTypeSlot` |
| `CalendarGroupSlotAvailability` | `AppointmentTypeSlotAvailability` |
| `CalendarGroupRangeAvailability` | `AppointmentTypeRangeAvailability` |
| `PaginatedCalendarGroupRangeAvailabilityList` | `PaginatedAppointmentTypeRangeAvailabilityList` |
| `CalendarGroupAvailabilityQuery` | `AppointmentTypeAvailabilityQuery` |
| `CalendarGroupEventCreate` | `AppointmentTypeEventCreate` |
| `BookingCodeGroupEventCreate` | `BookingCodeAppointmentTypeEventCreate` |
| `CalendarEventGroupSelection` | `CalendarEventAppointmentTypeSelection` |
| `_CalendarGroupSlotSelectionInput` | `_AppointmentTypeSlotSelectionInput` |
| `GroupScopedAvailabilityWindow` | `AppointmentTypeScopedAvailabilityWindow` |
| `GroupScopedAvailabilityWindowCreate` | `AppointmentTypeScopedAvailabilityWindowCreate` |
| `PatchedGroupScopedAvailabilityWindowUpdate` | `PatchedAppointmentTypeScopedAvailabilityWindowUpdate` |
| `PaginatedGroupScopedAvailabilityWindowList` | `PaginatedAppointmentTypeScopedAvailabilityWindowList` |
| `GroupScopedAvailabilityWriteResult` | `AppointmentTypeScopedAvailabilityWriteResult` |
| `GroupScopedAvailabilityOrphanedBooking` | `AppointmentTypeScopedAvailabilityOrphanedBooking` |
| `GroupScopedBlockedTime` | `AppointmentTypeScopedBlockedTime` |
| `GroupScopedBlockedTimeCreate` | `AppointmentTypeScopedBlockedTimeCreate` |
| `PatchedGroupScopedBlockedTimeUpdate` | `PatchedAppointmentTypeScopedBlockedTimeUpdate` |
| `PaginatedGroupScopedBlockedTimeList` | `PaginatedAppointmentTypeScopedBlockedTimeList` |
| `GroupScopedBlockWriteResult` | `AppointmentTypeScopedBlockWriteResult` |
| `GroupScopedBlockOrphanedBooking` | `AppointmentTypeScopedBlockOrphanedBooking` |
| `GroupScopedQuotaRule` | `AppointmentTypeScopedQuotaRule` |
| `GroupScopedQuotaRuleCreate` | `AppointmentTypeScopedQuotaRuleCreate` |
| `PatchedGroupScopedQuotaRuleUpdate` | `PatchedAppointmentTypeScopedQuotaRuleUpdate` |
| `PaginatedGroupScopedQuotaRuleList` | `PaginatedAppointmentTypeScopedQuotaRuleList` |

Nested field names inside the scoped schemas move with them: `group_slot_id` → `appointment_type_slot_id` on `AppointmentTypeScopedAvailabilityWindow`, `AppointmentTypeScopedBlockedTime` and `AppointmentTypeScopedQuotaRule`.

OpenAPI tags also change, which reorders generated SDK namespaces: `calendar-groups` → `appointment-types`, and `Calendar Group Scoped {Availability Windows,Blocked Times,Quota Rules}` → `Appointment Type Scoped …`.

### GET /appointment-types/ · POST /appointment-types/

- **Status:** changed (renamed from `/calendar-groups/`) — **breaking**
- **Auth:** session or token auth; `X-Organization-Id` header required. Admins see every appointment type in the organization; non-admin members see only those they participate in (own a calendar in a slot roster). Unchanged from before.
- **Query parameters (GET):** `X-Organization-Id` (header), `limit`, `offset`, `name`. Unchanged.

**`AppointmentType` shape**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `id` | integer | yes | read-only |
| `name` | string (≤255) | yes | |
| `description` | string | no | |
| `duration` | string | no | `HH:MM:SS`. Must be set when `accepts_public_scheduling` is true |
| `accepts_public_scheduling` | boolean | no | |
| `slots` | array\<`AppointmentTypeSlot`\> | yes | |
| `public_booking_slug` | string | yes | read-only; opaque identifier for the unauthenticated codeless booking route |
| `created`, `modified` | string (date-time) | yes | read-only |

**`AppointmentTypeSlot` shape**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `id` | integer | yes | read-only |
| `name` | string (≤255) | yes | |
| `description` | string | no | |
| `order` | integer (0–32767) | no | |
| `required_count` | integer (0–32767) | no | how many calendars must be picked from this slot |
| `calendars` | array\<`Calendar`\> | yes | read-only |
| `calendar_ids` | array\<integer\> | yes | write-only |
| `pools` | array\<`CalendarPool`\> | yes | read-only |
| `pool_ids` | array\<integer\> | no | write-only. Omitting leaves attachments unchanged; `[]` detaches all |

**Example — create** (captured from a running instance on this branch)

```http
POST /appointment-types/
X-Organization-Id: 1
Content-Type: application/json
```

```json
{
  "name": "Annual Physical",
  "description": "30-minute yearly checkup",
  "accepts_public_scheduling": true,
  "duration": "00:30:00",
  "slots": [
    { "name": "Physicians", "order": 0, "required_count": 1, "calendar_ids": [1] },
    { "name": "Rooms",      "order": 1, "required_count": 1, "calendar_ids": [2] }
  ]
}
```

`201 Created`:

```json
{
  "id": 1,
  "name": "Annual Physical",
  "description": "30-minute yearly checkup",
  "duration": "00:30:00",
  "accepts_public_scheduling": true,
  "slots": [
    {
      "id": 1,
      "name": "Physicians",
      "description": "",
      "order": 0,
      "required_count": 1,
      "calendars": [
        {
          "id": 1,
          "name": "Dr. Reyes",
          "description": "",
          "email": "",
          "external_id": "cal-reyes",
          "provider": "internal",
          "calendar_type": "personal",
          "capacity": null,
          "manage_available_windows": false,
          "visibility": "active",
          "sync_enabled": true
        }
      ],
      "pools": []
    },
    {
      "id": 2,
      "name": "Rooms",
      "description": "",
      "order": 1,
      "required_count": 1,
      "calendars": [
        {
          "id": 2,
          "name": "Room 2",
          "description": "",
          "email": "",
          "external_id": "cal-room-2",
          "provider": "internal",
          "calendar_type": "resource",
          "capacity": null,
          "manage_available_windows": false,
          "visibility": "active",
          "sync_enabled": true
        }
      ],
      "pools": []
    }
  ],
  "public_booking_slug": "EwMePkzFPBb14N-B8XGPsQ",
  "created": "2026-09-06T20:04:41.472491Z",
  "modified": "2026-09-06T20:04:41.472491Z"
}
```

`GET /appointment-types/` wraps the same objects in the standard `{count, next, previous, results}` envelope.

**Errors:** unchanged — `400` on validation (duplicate slot name, calendar shared across slots, `required_count` above pool size, public scheduling without a duration), `403` for a non-member, `404` for a member who does not participate.

**Client migration notes**
- *Web SPA (React):* change the base path and any generated hook/service name; response shape is otherwise identical.
- *Partner integrations:* same, plus the token scope rename in [Token scopes](#token-scopes).

### POST /appointment-types/{id}/events/

- **Status:** changed (renamed) — **breaking**
- **Request** (`AppointmentTypeEventCreate`): `title` (required), `start_time`, `end_time`, `timezone` (all required), `slot_selections` (required, array of `{slot_id, calendar_ids}`), `description`, `attendances`, `external_attendances`. Field names unchanged; only the schema component name moved.
- **Response:** `CalendarEvent` — **which now carries `appointment_type_selections` instead of `group_selections`.**

```json
{
  "title": "Physical — J. Okafor",
  "start_time": "2026-10-01T14:00:00Z",
  "end_time": "2026-10-01T14:30:00Z",
  "timezone": "America/Sao_Paulo",
  "slot_selections": [
    { "slot_id": 1, "calendar_ids": [1] },
    { "slot_id": 2, "calendar_ids": [2] }
  ]
}
```

### POST /appointment-types/{id}/availability/

- **Status:** changed (renamed) — **breaking**
- **Request** (`AppointmentTypeAvailabilityQuery`): `{"ranges": [{"start_time": …, "end_time": …}, …]}` — unchanged.
- **Response:** array of `AppointmentTypeRangeAvailability`:

```json
[
  {
    "start_time": "2026-10-01T09:00:00Z",
    "end_time": "2026-10-01T17:00:00Z",
    "slots": [
      { "slot_id": 1, "available_calendar_ids": [1], "required_count": 1, "is_bookable": true },
      { "slot_id": 2, "available_calendar_ids": [], "required_count": 1, "is_bookable": false }
    ]
  }
]
```

### Appointment-type-scoped windows / blocked times / quota rules

Nested under `/appointment-types/{appointment_type_id}/slots/{slot_id}/…`. Full CRUD, unchanged semantics. The one field rename inside the payloads is `group_slot_id` → `appointment_type_slot_id`.

`AppointmentTypeScopedQuotaRule`: `id`, `calendar_id`, `appointment_type_slot_id`, `period` (`PeriodEnum`), `cap`, `created`, `modified` — all read-only on read.

`AppointmentTypeScopedAvailabilityWindow`: `id`, `calendar_id`, `appointment_type_slot_id`, `start_time`, `end_time`, `timezone`, `rrule_string` (nullable), `is_recurring`, `created`, `modified`.

Create/update on windows and blocked times return a write result carrying the row plus any bookings the write orphaned:

```json
{
  "window": { "id": 7, "calendar_id": 1, "appointment_type_slot_id": 1, "…": "…" },
  "orphaned_bookings": []
}
```

### Public (codeless) booking routes

`/public/booking/appointment-types/{public_slug}/{events,bookable-slots,availability}/` — unauthenticated, addressed by the appointment type's `public_booking_slug`, never by its integer id. Behaviour unchanged; only the path segment moved.

The code-gated variants `/public/booking/appointment-type-{availability,bookable-slots}/` take the code in the `X-Booking-Code` header exactly as before.

`POST /public/booking/appointment-type-events/reschedule/` (was `/public/booking/group-events/reschedule/`) — note the singular `appointment-type-events` segment.

Booking through a code uses `BookingCodeAppointmentTypeEventCreate`: `title`, `start_time`, `end_time`, `timezone`, `slot_selections`, `external_attendee` (all required), `description` (optional).

---

## GraphQL

Endpoint, auth and error envelope are unchanged. Only names moved.

### Query fields

| Before | After | Argument change |
| --- | --- | --- |
| `calendarGroup(groupId:)` | `appointmentType(appointmentTypeId:)` | `groupId` → `appointmentTypeId` |
| `calendarGroups(offset:, limit:)` | `appointmentTypes(offset:, limit:)` | none |
| `calendarGroupAvailability(groupId:, ranges:)` | `appointmentTypeAvailability(appointmentTypeId:, ranges:)` | `groupId` → `appointmentTypeId` |
| `calendarGroupBookableSlots(groupId:, …)` | `appointmentTypeBookableSlots(appointmentTypeId:, …)` | `groupId` → `appointmentTypeId` |
| `calendarGroupEvents(groupId:, startDatetime:, endDatetime:)` | `appointmentTypeEvents(appointmentTypeId:, …)` | `groupId` → `appointmentTypeId` |
| `calendarGroupStaleSelections(groupId:, …)` | `appointmentTypeStaleSelections(appointmentTypeId:, …)` | `groupId` → `appointmentTypeId` |
| `groupScopedAvailabilityWindows(groupSlotId:, calendarId:, …)` | `appointmentTypeScopedAvailabilityWindows(appointmentTypeSlotId:, …)` | `groupSlotId` → `appointmentTypeSlotId` |
| `groupScopedBlockedTimes(groupSlotId:, …)` | `appointmentTypeScopedBlockedTimes(appointmentTypeSlotId:, …)` | `groupSlotId` → `appointmentTypeSlotId` |
| `groupScopedQuotaRules(groupSlotId:, …)` | `appointmentTypeScopedQuotaRules(appointmentTypeSlotId:, …)` | `groupSlotId` → `appointmentTypeSlotId` |
| `calendarGroupBookableSlotsWithCode(code:, …)` | `appointmentTypeBookableSlotsWithCode(code:, …)` | none |
| `calendarGroupAvailabilityWithCode(code:, ranges:)` | `appointmentTypeAvailabilityWithCode(code:, ranges:)` | none |

`bookingPolicies` keeps its name but its filter argument moved: `calendarGroupId` → `appointmentTypeId`.

### Mutation fields

| Before | After |
| --- | --- |
| `createCalendarGroup(input: CalendarGroupInput!)` | `createAppointmentType(input: AppointmentTypeInput!)` |
| `updateCalendarGroup(input: UpdateCalendarGroupInput!)` | `updateAppointmentType(input: UpdateAppointmentTypeInput!)` |
| `deleteCalendarGroup(input: DeleteCalendarGroupInput!)` | `deleteAppointmentType(input: DeleteAppointmentTypeInput!)` |
| `createCalendarGroupEvent(input: CalendarGroupEventInput!)` | `createAppointmentTypeEvent(input: AppointmentTypeEventInput!)` |
| `createCalendarGroupBookingCode(input: CreateGroupBookingCodeInput!)` | `createAppointmentTypeBookingCode(input: CreateAppointmentTypeBookingCodeInput!)` |
| `createCalendarGroupRescheduleBookingCode(input: CreateGroupEventCodeInput!)` | `createAppointmentTypeRescheduleBookingCode(input: CreateAppointmentTypeEventCodeInput!)` |
| `createCalendarGroupCancellationBookingCode(input: CreateGroupEventCodeInput!)` | `createAppointmentTypeCancellationBookingCode(input: CreateAppointmentTypeEventCodeInput!)` |
| `createCalendarGroupEventWithCode(input: CreateGroupEventWithCodeInput!)` | `createAppointmentTypeEventWithCode(input: CreateAppointmentTypeEventWithCodeInput!)` |
| `rescheduleCalendarGroupEventWithCode(input: RescheduleGroupWithCodeInput!)` | `rescheduleAppointmentTypeEventWithCode(input: RescheduleAppointmentTypeWithCodeInput!)` |
| `rescheduleCalendarGroupEvent(input: …)` | `rescheduleAppointmentTypeEvent(input: RescheduleAppointmentTypeEventInput!)` |
| `batchUpsertGroupScopedAvailabilityWindows(…)` | `batchUpsertAppointmentTypeScopedAvailabilityWindows(…)` |
| `batchUpsertGroupScopedBlockedTimes(…)` | `batchUpsertAppointmentTypeScopedBlockedTimes(…)` |
| `batchUpsertGroupScopedQuotaRules(…)` | `batchUpsertAppointmentTypeScopedQuotaRules(…)` |

### GraphQL types

| Before | After |
| --- | --- |
| `CalendarGroupGraphQLType` | `AppointmentTypeGraphQLType` |
| `CalendarGroupSlotGraphQLType` | `AppointmentTypeSlotGraphQLType` |
| `CalendarGroupSlotAvailabilityGraphQLType` | `AppointmentTypeSlotAvailabilityGraphQLType` |
| `CalendarGroupRangeAvailabilityGraphQLType` | `AppointmentTypeRangeAvailabilityGraphQLType` |
| `CalendarEventGroupSelectionGraphQLType` | `CalendarEventAppointmentTypeSelectionGraphQLType` |
| `GroupScopedAvailabilityWindowGraphQLType` | `AppointmentTypeScopedAvailabilityWindowGraphQLType` |
| `GroupScopedBlockedTimeGraphQLType` | `AppointmentTypeScopedBlockedTimeGraphQLType` |
| `GroupScopedQuotaRuleGraphQLType` | `AppointmentTypeScopedQuotaRuleGraphQLType` |
| `CalendarGroupResult` | `AppointmentTypeResult` |
| `CalendarGroupEventResult` | `AppointmentTypeEventResult` |
| `DeleteCalendarGroupResult` | `DeleteAppointmentTypeResult` |

`AppointmentTypeResult`'s payload field is renamed too: `{ success, group, errorMessage }` → `{ success, appointmentType, errorMessage }`.

```graphql
type AppointmentTypeGraphQLType {
  id: ID!
  name: String!
  description: String!
  created: DateTime!
  modified: DateTime!
  slots: [AppointmentTypeSlotGraphQLType!]!
  isPrivate: Boolean!
}
```

### GraphQL inputs

| Before | After | Field change |
| --- | --- | --- |
| `CalendarGroupInput` | `AppointmentTypeInput` | none — `organizationId`, `name`, `description`, `slots`, `isPrivate`, `durationSeconds` |
| `UpdateCalendarGroupInput` | `UpdateAppointmentTypeInput` | `groupId` → `appointmentTypeId` |
| `DeleteCalendarGroupInput` | `DeleteAppointmentTypeInput` | `groupId` → `appointmentTypeId` |
| `CalendarGroupEventInput` | `AppointmentTypeEventInput` | `groupId` → `appointmentTypeId` |
| `CalendarGroupSlotInput` | `AppointmentTypeSlotInput` | none |
| `CalendarGroupSlotSelectionInput` | `AppointmentTypeSlotSelectionInput` | none — `slotId`, `calendarIds` |
| `CreateGroupBookingCodeInput` | `CreateAppointmentTypeBookingCodeInput` | `calendarGroupId` → `appointmentTypeId` |
| `CreateGroupEventCodeInput` | `CreateAppointmentTypeEventCodeInput` | `calendarGroupId` → `appointmentTypeId` |
| `CreateGroupEventWithCodeInput` | `CreateAppointmentTypeEventWithCodeInput` | none |
| `RescheduleGroupWithCodeInput` | `RescheduleAppointmentTypeWithCodeInput` | none |
| `BatchGroupScoped*Input` | `BatchAppointmentTypeScoped*Input` | `groupSlotId` → `appointmentTypeSlotId` |

### GraphQL type fields

Renamed on types you query in many places:

| Type | Before | After |
| --- | --- | --- |
| `CalendarEventGraphQLType` | `calendarGroup` | `appointmentType` |
| `CalendarEventGraphQLType` | `groupSelections` | `appointmentTypeSelections` |
| `BookingPolicyGraphQLType` | `calendarGroupId` | `appointmentTypeId` |
| `ChildOrganizationMetrics` | `calendarGroupCount` | `appointmentTypeCount` |

### GraphQL example

Before:

```graphql
mutation CreateCalendarGroup($input: CalendarGroupInput!) {
  createCalendarGroup(input: $input) {
    success
    errorMessage
    group { id name }
  }
}
```

After:

```graphql
mutation CreateAppointmentType($input: AppointmentTypeInput!) {
  createAppointmentType(input: $input) {
    success
    errorMessage
    appointmentType { id name }
  }
}
```

Variables (unchanged in shape):

```json
{
  "input": {
    "organizationId": 1,
    "name": "Annual Physical",
    "description": "30-minute yearly checkup",
    "isPrivate": false,
    "durationSeconds": 1800,
    "slots": [
      { "name": "Physicians", "order": 0, "requiredCount": 1, "calendarIds": [1] },
      { "name": "Rooms",      "order": 1, "requiredCount": 1, "calendarIds": [2] }
    ]
  }
}
```

---

## Other contract changes

### Token scopes

`available_resources` on `POST /public-api-tokens/` and `PATCH /public-api-tokens/{id}/`, and the values listed by `GET /public-api-docs/scopes/`:

| Before | After |
| --- | --- |
| `calendar_group` | `appointment_type` |
| `group_scoped_availability_windows` | `appointment_type_scoped_availability_windows` |
| `batch_upsert_group_scoped_availability_windows` | `batch_upsert_appointment_type_scoped_availability_windows` |
| `group_scoped_blocked_times` | `appointment_type_scoped_blocked_times` |
| `batch_upsert_group_scoped_blocked_times` | `batch_upsert_appointment_type_scoped_blocked_times` |
| `group_scoped_quota_rules` | `appointment_type_scoped_quota_rules` |
| `batch_upsert_group_scoped_quota_rules` | `batch_upsert_appointment_type_scoped_quota_rules` |

**Existing tokens are migrated server-side** and keep working without any client action. Only clients that *write* scopes need to change what they send. Sending an old value now fails validation.

### Billing resource key

`resource_key` (`ResourceKeyEnum`): `calendar_groups` → `appointment_types`. Surfaces on `/billing/add-ons/`, `/billing/add-ons/{id}/` and `/billing/subscription/change-plan/`, via the `PlanLimit`, `SubscriptionAddOn` and `AddOnPurchaseRequest` schemas. Existing rows are migrated; clients that hard-code the key when purchasing an add-on must update it.

### Client migration notes by platform

*Web SPA (React)*
1. Regenerate the REST client from the new `schema.yml` — path, operation ID and tag names all move.
2. Update every GraphQL document: field names, argument names, input type names, and the four renamed type fields. A stale field fails the whole document at validation, so a compile-time check against the new schema is the fastest way to find them all.
3. Rename local reads of `group_selections` → `appointment_type_selections` on calendar events, and `calendar_group` → `appointment_type` on booking policies.
4. If any UI copy says "calendar group", the product term is now "appointment type".

*Partner integrations*
1. Everything above, plus: if you provision or update API tokens programmatically, send the new `available_resources` values. Tokens you already hold keep their grants.
2. If you purchase billing add-ons by `resource_key`, send `appointment_types`.
3. If you match on error `detail` text, switch to `error_code` — the codes did not change and the text did.

---

## Rollout

- **Not yet live.** The change sits on PR #330 against `main`, not merged and not deployed.
- **No feature flag and no compatibility window.** The old names are removed in the same release that adds the new ones, so client and API changes must ship together.
- **Sequencing:** staging first. Because there is no overlap, expect a brief window on each environment where an un-updated client sees 404s (REST) or validation errors (GraphQL). Coordinate the deploy with each client team rather than rolling the API ahead of them.
- **No client-side data migration.** Server-side migrations repoint stored token scopes and billing resource keys automatically; no ids change, so anything you cached by appointment-type id stays valid.

---

## Provenance

Derived from the diff `5ed761bd..bab58799`, primarily by diffing the drf-spectacular OpenAPI spec (`schema.yml`) on both revisions and the Strawberry schema for the GraphQL half. REST examples were captured from a live instance running this branch. Backend paths are cited only as provenance; nothing here requires access to that repo.
