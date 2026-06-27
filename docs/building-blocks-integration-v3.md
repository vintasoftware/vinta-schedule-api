# BuildingBlocks \<-\> Vinta-Schedule Integration Project (v3)

The objective of this document is to explain how the integration is going to work and what parts are still missing in the Vinta-Schedule APIs to make sure the integration is covered.

> **v3 note.** Every gap v2 listed has now shipped to `main` — **the integration surface is fully covered**, with no 🔶/❌ API items left. This revision re-checks every call against the **actual current schema** (`public_api/queries.py`, `public_api/mutations.py`, `calendar_integration/graphql.py`, `calendar_integration/mutations.py`, `public_api/permissions.py`, `public_api/constants.py`) and against the implementation plans under `ai-plans/`. Signatures in the "Integration touch-points per screen" section reflect real field/argument names, not proposals. See **[What changed since v2](#what-changed-since-v2)** for the diff.

## Status legend

Each API call below is tagged with one of:

| Tag | Meaning |
| :---- | :---- |
| ✅ **Ready** | Query/mutation exists in the Public GraphQL API (or REST, where noted) on `main`. |
| 🔶 **Service ready / GraphQL missing** | Business logic exists in a service, but there is **no** Public API field exposing it. |
| ❌ **Missing** | Neither API nor service support exists; net-new work. |

### Schema conventions (important)

- The Public API uses **plain lists with `offset`/`limit` pagination**, *not* Relay `connection { nodes { ... } }` envelopes. The `nodes { ... }` syntax used in v1 of this doc does **not** match the real schema.
- Strawberry auto-camelCases fields, so Python `start_datetime` is `startDatetime` in GraphQL.
- Authenticated Public API fields are guarded by `IsAuthenticated` + `OrganizationResourceAccess`. Adding a field also requires an entry in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` (`public_api/permissions.py`) and, for new resource categories, a value in `PublicAPIResources` (`public_api/constants.py`).
- **Token scopes (new since v2).** A `SystemUser` token is now either:
  - **Org-wide** — `scoped_to_membership` is null; sees/acts on all org resources of the granted types. Minted via REST `POST /public-api-tokens/` or GraphQL `createSystemUserToken`.
  - **Provider-scoped (per-owner)** — `scoped_to_membership` points at one member; reads are filtered and writes are guarded to **only that provider's calendars** (`public_api/scoping.py`). Minted via GraphQL `createScopedSystemUser`. Its grantable resources are limited to the `PROVIDER_SCOPED_RESOURCES` allow-list.
- **Membership-based identity (new since v2).** People are now exposed as **memberships**, not bare users. Calendar owners resolve to `owners { id isDefault membership { userId organizationId role } }` and event attendees to `attendeeMemberships { userId organizationId role }` + `externalAttendees { id email name }`. The v2 `owners { user { profile { ... } } }` / `attendees { id email }` shapes do **not** exist.
- **Privacy is `isPrivate` (new since v2).** `Calendar`, `CalendarGroup`, and bundle calendars all expose a read-only **`isPrivate`** boolean, derived as `not accepts_public_scheduling`. It is accepted on the create/update inputs as `isPrivate`. **Default is private** (`isPrivate = true`); codeless public scheduling is opt-in.
- **Patients don't get tokens.** The patient path is unauthenticated and authorized entirely by a **single-use booking code** (`*WithCode` queries/mutations). No `IsAuthenticated`/`OrganizationResourceAccess` runs on those fields.

---

## What changed since v2

Implementation plans that **landed** (verified in code on `main`):

| v2 gap | Status in v3 | What shipped | Plan |
| :---- | :---- | :---- | :---- |
| Single-calendar availability / blocked-time mutations | ✅ Ready | `createAvailabilityWindow`, `updateAvailabilityWindow`, `deleteAvailabilityWindow`, `batchUpdateAvailabilityWindows`, `createBlockedTime`, `updateBlockedTime`, `deleteBlockedTime` | `PUBLIC_GRAPHQL_SERVICE_WRAPPERS` |
| Resource-calendar mutations | ✅ Ready | `createResourceCalendar`, `disableResourceCalendar`, `importResourceCalendars`, plus generic `createCalendar` / `updateCalendar` | `PUBLIC_GRAPHQL_SERVICE_WRAPPERS` + `UNIFORM_ACCEPTS_PUBLIC_SCHEDULING` |
| Bundles GraphQL surface | ✅ Ready | `calendarBundles` query + `createCalendarBundle` / `updateCalendarBundle` / `disableCalendarBundle` | `PUBLIC_GRAPHQL_SERVICE_WRAPPERS` |
| `owners` on calendars/bundles | ✅ Ready (membership shape) | `owners { id isDefault membership { userId organizationId role } }` | `CALENDAR_OWNERS_GRAPHQL_FIELD` + `MEMBERSHIP_SCOPED_CALENDAR_REFERENCES` |
| `isPrivate` across calendar/group/bundle | ✅ Ready | derived `isPrivate` field + `isPrivate` inputs; group public booking gated on it | `UNIFORM_ACCEPTS_PUBLIC_SCHEDULING` |
| `userId` argument on `calendarEvents` | ✅ Ready | `calendarEvents(userId: Int, ...)` | `CALENDAR_EVENTS_USER_FILTER` |
| Single-use scheduling/booking codes (GraphQL) | ✅ Ready | 6 mint mutations + `revokeBookingCode`, 5 `*WithCode` reads, 5 `*WithCode` actions | `SINGLE_USE_SCHEDULING_CODES` |
| Per-user / patient-scoped tokens (§3.2) | ✅ Ready | `createScopedSystemUser` + `scoped_to_membership` scoping enforced on reads **and** writes; `scheduleEvent` for owner-scoped event creation | `PER_OWNER_SCOPED_PUBLIC_API_TOKENS` + `..._WRITES` |
| Authenticated provider reschedule/cancel | ✅ Ready | `rescheduleCalendarEvent`, `rescheduleCalendarGroupEvent`, `cancelEvent` (owner-scope guarded; recurrence/series aware) | service-wrapper follow-up |
| Resource-calendar capacity edit | ✅ Ready | `updateResourceCalendar` (name/description/**capacity**/manageAvailableWindows/isPrivate/visibility; INTERNAL resource calendars only) | service-wrapper follow-up |
| `user_created` webhook (§2.1) | ✅ Ready (**renamed**) | event type is **`organization_member_created`**, not `user_created`; webhook config now manageable over GraphQL too | `ORGANIZATION_MEMBER_CREATED_WEBHOOK` |

New capabilities that didn't exist as concepts in v2:

- **External-event change-request flow** — inbound Google edits/deletes can be routed to approval per org (`Organization.external_event_update_policy` ∈ `allow` / `change_request` / `forbidden`, default `change_request`). Surfaced as `externalEventChangeRequests` query + `approveExternalEventChangeRequest` / `rejectExternalEventChangeRequest` mutations. Relevant to event sync (§4). Plan: `EXTERNAL_EVENT_UPDATE_POLICY`.
- **ICS export** — `eventIcs(eventId: Int!): String` GraphQL query + `GET /calendar-events/{id}/ics/` REST. Plan: `CALENDAR_EVENT_ICS_EXPORT`.
- **Whitelabel / reseller surface** — `createOrganization`, `createInvitation`, `createSystemUserToken`, `updateBranding`, `childOrganizations`, and unauthenticated `brandingForTenant` / `validateReturnUrl`. Plan: `WHITELABEL_API_PROVISIONING`.

**Every API gap from v2 is now closed.** No 🔶/❌ items remain on the integration surface — see **[What needs to be done](#what-needs-to-be-done-gap-summary)**, which now lists only integration-side decisions.

---

# Integration setup questions

## 1\. How are admins going to connect to vinta-schedule?

### 1.1. create an account in vinta-schedule — ✅ Ready

Sign up and create an organization using email/password or social login (Google).

### 1.2. configure the service accounts — ✅ Ready (admin UI / existing flows)

Service accounts are necessary to sync room/resource calendars with Google Calendar and do some admin operations on Google Calendar integration.

### 1.3. \[optional\] import resource calendars — ✅ Ready (was 🔶 in v2)

Resource import from Google Workspace is now exposed as `importResourceCalendars` (async enqueue). See Location Page.

## 2\. How do admins invite team members to vinta-schedule?

### 2.1. Notify Medplum when a member is created — ✅ Ready (was ❌; **renamed**)

The outgoing webhook event exists, but it is **`organization_member_created`** (not `user_created`). It fires when a user accepts an org invitation or creates a new org — i.e. on **membership creation**, which is the correct signal in the multi-tenant model. Payload: `{ user_id, email, organization_id, organization_name, membership_role, membership_id }`, wrapped in the standard envelope `{ id, type, timestamp, data }`. Webhook configs can now be managed over **GraphQL** (`webhookConfigurations` + `create/update/deleteWebhookConfiguration`) in addition to REST (`/webhooks/`).

> Note: all outgoing webhook payloads are now enveloped (`{ id, type, timestamp, data }`) — a breaking change vs. the pre-envelope `calendar_event_*` payloads.

### 2.2. invite the user on vinta-schedule — ✅ Ready

Providers are invited to Vinta Schedule so they authorize calendar access in Google Calendar. Reseller integrations can mint invitations via `createInvitation`.

### 2.3. user accepts the invitation — ✅ Ready (triggers `organization_member_created`)

### 2.4. membership id is linked to the medplum provider — ✅ Ready (was ❌)

On `organization_member_created`, a Medplum bot links the vinta-schedule identifier to the Provider and mints a provider-scoped token via `createScopedSystemUser` (§3.2).

### 2.5. user configures their calendars — ✅ mostly Ready

Default calendar, listing, and auto-sync are backed by `Calendar.visibility`, `Calendar.sync_enabled`, `accepts_public_scheduling` (exposed as `isPrivate`), and `CalendarOwnership.is_default`. The `isDefault` flag is now visible via the `owners` field. Some self-service *mutations* to flip these from the integration may still be thin.

## 3\. How does the admin integrate medplum with vinta-schedule?

### 3.1. Create an admin Public API token — ✅ Ready (REST **and** GraphQL)

- **REST** `POST /public-api-tokens/` (`integration_name` + `available_resources: [...]`), gated by `IsOrganizationAdmin`. Plaintext token returned **once** as `{system_user.id}:{token}`.
- **GraphQL** `createSystemUserToken(input: { organizationId, integrationName, resources })` for reseller bundles (gated by the `SYSTEM_USER` resource + subtree check).

### 3.2. Create per-provider tokens — ✅ Ready (was ❌)

`createScopedSystemUser(input: { integrationName, scopedToUserId, availableResources })` mints a **provider-scoped** token. `scopedToUserId` is resolved to that user's active membership and stored as `SystemUser.scoped_to_membership`. The token's reads are filtered and its writes guarded to that provider's calendars only (`public_api/scoping.py`, `assert_calendar_in_owner_scope`). `availableResources` must be drawn from the `PROVIDER_SCOPED_RESOURCES` allow-list (recurring/specific availability, blocked times, free/busy reads, list events, **`scheduleEvent`**).

```graphql
mutation CreateScopedSystemUser($input: CreateScopedSystemUserInput!) {
  # input: { integrationName, scopedToUserId, availableResources: [String!]! }
  createScopedSystemUser(input: $input) {
    id integrationName isActive availableResources scopedToUserId token
  }
}
```

### 3.3. Patient booking — ✅ Ready via single-use codes (was ❌)

The design chose **codes, not patient tokens**: a patient never holds an org token. An admin/provider mints a single-use code (§3.4) and the patient acts with it through the unauthenticated `*WithCode` fields. Restricted vs. public is governed by `isPrivate` (private calendars/groups/bundles require a code; public ones accept codeless booking).

### 3.4. Single-use scheduling codes — ✅ Ready (was ❌)

Backed by `CalendarManagementToken` + `CalendarManagementTokenPermission`. Mint mutations (org-token-gated, resource `CALENDAR_BOOKING_CODE`): `createCalendarBookingCode`, `createCalendarGroupBookingCode`, `createCalendarRescheduleBookingCode`, `createCalendarGroupRescheduleBookingCode`, `createCalendarCancellationBookingCode`, `createCalendarGroupCancellationBookingCode`, plus `revokeBookingCode`. The plaintext `code` is returned once. Reschedule/cancel codes are bound to one specific `eventId`. See the Booking Link Creation and Patient Portal sections.

## 4\. How do the events get synchronized between VintaSchedule and the Building Blocks?

When an appointment is created on Building Blocks, create a `CalendarEvent` on VintaSchedule and store its id on the Appointment as an identifier.

Updates flowing the other way use **outgoing webhooks**, which exist for `calendar_event_created/updated/deleted` and attendee changes (now enveloped), so Medplum Bots can subscribe today. The `organization_member_created` event (§2.1) is also available.

**New in v3 — inbound conflict handling.** Edits/deletes made directly in Google Calendar are governed per org by `Organization.external_event_update_policy`:

- `allow` — applied directly.
- `change_request` (**default**) — routed to an `ExternalEventChangeRequest` for approval.
- `forbidden` — auto-undone (pushed back to the provider).

Integrations can drive the approval queue via `externalEventChangeRequests` (query) and `approveExternalEventChangeRequest` / `rejectExternalEventChangeRequest` (mutations, provider-scoped tokens only). This matters if Building Blocks is the source of truth and you want provider-side Google edits gated.

---

# Integration touch-points per screen

Each call lists its **status tag** and a concrete GraphQL signature reflecting the **current** schema. Every call is now ✅ Ready; the "(was 🔶/❌)" notes record what changed since v2.

## Provider/Admin App

### Login / SSO

- **List calendars** — ✅ Ready (now exposes `isPrivate` + `owners`)

```graphql
query Calendars($userId: Int, $calendarType: String, $offset: Int! = 0, $limit: Int! = 100) {
  calendars(userId: $userId, calendarType: $calendarType, offset: $offset, limit: $limit) {
    id name description email calendarType capacity visibility syncEnabled isPrivate
    owners { id isDefault membership { userId organizationId role } }
  }
}
```

### Location Page

- **List resources** — ✅ Ready (filter by type)

```graphql
query Resources($offset: Int! = 0, $limit: Int! = 100) {
  calendars(calendarType: "resource", offset: $offset, limit: $limit) {
    id name description capacity calendarType isPrivate
  }
}
```

- **createResourceCalendar** — ✅ Ready (was 🔶)

```graphql
mutation CreateResourceCalendar($input: CreateResourceCalendarInput!) {
  # input: { organizationId, name, description, capacity, manageAvailableWindows, isPrivate }
  createResourceCalendar(input: $input) {
    success errorMessage calendar { id name description capacity calendarType isPrivate }
  }
}
```

- **editResourceCalendar** — ✅ Ready (was 🔶 for capacity in the previous draft)

  Use the dedicated `updateResourceCalendar` mutation (resource `UPDATE_RESOURCE_CALENDAR`). It is a partial update — only provided fields change — and now includes **`capacity`** (integer sets it, explicit `null` clears to unlimited, omit to leave unchanged). The target must be a `RESOURCE`-type calendar with provider `INTERNAL` (Google-synced resource calendars are rejected). A generic `updateCalendar` (name/description/isPrivate) also exists for non-resource calendars.

```graphql
mutation UpdateResourceCalendar($input: UpdateResourceCalendarInput!) {
  # input: { organizationId, calendarId, name, description, capacity, manageAvailableWindows, isPrivate, visibility }
  updateResourceCalendar(input: $input) {
    success errorMessage calendar { id name description capacity isPrivate }
  }
}
```

- **disableResourceCalendar** — ✅ Ready (was 🔶; sets `visibility = INACTIVE`)

```graphql
mutation DisableResourceCalendar($input: DisableResourceCalendarInput!) {  # { organizationId, calendarId }
  disableResourceCalendar(input: $input) { success errorMessage }
}
```

- *(optional)* **importResourceCalendars** — ✅ Ready (was 🔶; async, no payload)

```graphql
mutation ImportResourceCalendars($input: ImportResourceCalendarsInput!) {  # { organizationId, startTime, endTime }
  importResourceCalendars(input: $input) { success errorMessage }
}
```

### Appointment Types & Calendar Groups & Bundles (Admin)

- **List calendar groups** — ✅ Ready (now with `isPrivate`; owners via slot calendars)

```graphql
query CalendarGroups($offset: Int! = 0, $limit: Int! = 100) {
  calendarGroups(offset: $offset, limit: $limit) {
    id name description isPrivate
    slots {
      id name requiredCount order
      calendars {
        id name
        owners { id isDefault membership { userId organizationId role } }
      }
    }
  }
}
```

- **List calendar bundles** — ✅ Ready (was ❌)

```graphql
query CalendarBundles($offset: Int! = 0, $limit: Int! = 100) {
  calendarBundles(offset: $offset, limit: $limit) {
    id name description isPrivate
    children { id name owners { id isDefault membership { userId organizationId role } } }
  }
}
```

- **createCalendarGroup** — ✅ Ready (`isPrivate` now supported)

```graphql
mutation CreateCalendarGroup($input: CalendarGroupInput!) {
  # input: { organizationId, name, description, isPrivate, slots: [{ slotId, name, calendarIds, requiredCount, description, order }] }
  createCalendarGroup(input: $input) { success errorMessage group { id name isPrivate } }
}
```

- **updateCalendarGroup** — ✅ Ready (`isPrivate` now supported)

```graphql
mutation UpdateCalendarGroup($input: UpdateCalendarGroupInput!) {
  # input: { organizationId, groupId, name, description, isPrivate, slots: [...] }
  updateCalendarGroup(input: $input) { success errorMessage group { id name isPrivate } }
}
```

- **disableCalendarGroup** — ✅ Ready (as `deleteCalendarGroup`)

```graphql
mutation DeleteCalendarGroup($input: DeleteCalendarGroupInput!) {  # { organizationId, groupId }
  deleteCalendarGroup(input: $input) { success errorMessage }
}
```

- **List calendars, filter by user** — ✅ Ready

```graphql
query CalendarsByUser($userId: Int!) { calendars(userId: $userId) { id name calendarType isPrivate } }
```

- **createCalendarBundle** — ✅ Ready (was 🔶)

```graphql
mutation CreateCalendarBundle($input: CreateCalendarBundleInput!) {
  # input: { organizationId, name, description, childrenIds: [Int!]!, primaryCalendarId, isPrivate }
  createCalendarBundle(input: $input) { success errorMessage bundle { id name isPrivate } }
}
```

- **updateCalendarBundle** — ✅ Ready (was 🔶)

```graphql
mutation UpdateCalendarBundle($input: UpdateCalendarBundleInput!) {
  # input: { organizationId, bundleId, name, description, childrenIds, primaryCalendarId, isPrivate }
  updateCalendarBundle(input: $input) { success errorMessage bundle { id name isPrivate } }
}
```

- **disableCalendarBundle** — ✅ Ready (was 🔶; `visibility = INACTIVE`)

```graphql
mutation DisableCalendarBundle($input: DisableCalendarBundleInput!) {  # { organizationId, bundleId }
  disableCalendarBundle(input: $input) { success errorMessage }
}
```

### Provider Availability

- **List available times** — ✅ Ready

```graphql
query AvailableTimes($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  availableTimes(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime recurrenceRule { rruleString }
  }
}
```

- **List unavailable times** — ✅ Ready

```graphql
query UnavailableWindows($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  unavailableWindows(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime reason
  }
}
```

- **List AvailabilityWindows** — ✅ Ready

```graphql
query AvailabilityWindows($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  availabilityWindows(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime canBookPartially
  }
}
```

- **List BlockedTimes** — ✅ Ready

```graphql
query BlockedTimes($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  blockedTimes(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime recurrenceRule { rruleString }
  }
}
```

- **createAvailabilityWindow** — ✅ Ready (was 🔶)

```graphql
mutation CreateAvailabilityWindow($input: CreateAvailableTimeInput!) {
  # input: { organizationId, calendarId, startTime, endTime, timezone, rruleString }
  createAvailabilityWindow(input: $input) { success errorMessage availableTime { id startTime endTime } }
}
```

- **createBlockedTime** — ✅ Ready (was 🔶)

```graphql
mutation CreateBlockedTime($input: CreateBlockedTimeInput!) {
  # input: { organizationId, calendarId, startTime, endTime, timezone, reason, rruleString }
  createBlockedTime(input: $input) { success errorMessage blockedTime { id startTime endTime } }
}
```

- **updateAvailabilityWindow** — ✅ Ready (was 🔶)

```graphql
mutation UpdateAvailabilityWindow($input: UpdateAvailableTimeInput!) {
  # input: { organizationId, calendarId, availableTimeId, startTime, endTime, timezone, rruleString }
  updateAvailabilityWindow(input: $input) { success errorMessage availableTime { id startTime endTime } }
}
```

- **batchUpdateAvailabilityWindows** — ✅ Ready (was 🔶; atomic)

```graphql
mutation BatchUpdateAvailabilityWindows($input: BatchAvailabilityInput!) {
  # input: { organizationId, calendarId, operations: [{ action: "create"|"update"|"delete", availableTimeId, startTime, endTime, timezone, rruleString }] }
  batchUpdateAvailabilityWindows(input: $input) { success errorMessage availableTimes { id startTime endTime } }
}
```

- **updateBlockedTime** — ✅ Ready (was 🔶)

```graphql
mutation UpdateBlockedTime($input: UpdateBlockedTimeInput!) {
  # input: { organizationId, calendarId, blockedTimeId, startTime, endTime, timezone, reason, rruleString }
  updateBlockedTime(input: $input) { success errorMessage blockedTime { id startTime endTime } }
}
```

- **deleteAvailabilityWindow** — ✅ Ready (was 🔶)

```graphql
mutation DeleteAvailabilityWindow($input: DeleteAvailableTimeInput!) {  # { organizationId, calendarId, availableTimeId }
  deleteAvailabilityWindow(input: $input) { success errorMessage }
}
```

- **deleteBlockedTime** — ✅ Ready (was 🔶)

```graphql
mutation DeleteBlockedTime($input: DeleteBlockedTimeInput!) {  # { organizationId, calendarId, blockedTimeId }
  deleteBlockedTime(input: $input) { success errorMessage }
}
```

### Scheduler / Calendar

- **List events (filter by user and/or calendar)** — ✅ Ready (now accepts `userId`)

```graphql
query CalendarEvents($calendarId: Int, $userId: Int, $start: DateTime!, $end: DateTime!) {
  calendarEvents(calendarId: $calendarId, userId: $userId, startDatetime: $start, endDatetime: $end) {
    id title description startTime endTime
    attendeeMemberships { userId organizationId role }
    externalAttendees { id email name }
    resources { id name }
  }
}
```

  The `userId` argument now filters directly to events on calendars owned by that user (was a v2 gap, B-8). Internal attendees are exposed as `attendeeMemberships` (not `attendees { id email }`). Match appointments to clinical info on the Building Blocks side, keyed by the stored CalendarEvent id.

### Create Appointment Modal

- **List resources** — ✅ Ready (`calendars(calendarType: "resource")`).
- **List calendar available times** — ✅ Ready (`availabilityWindows`).
- **List user available times** — ✅ Ready (resolve user calendar → `availabilityWindows`).
- **List calendar group available times** — ✅ Ready

```graphql
query GroupBookableSlots($groupId: Int!, $start: DateTime!, $end: DateTime!, $durationSeconds: Int!) {
  calendarGroupBookableSlots(groupId: $groupId, searchWindowStart: $start,
    searchWindowEnd: $end, durationSeconds: $durationSeconds) { startTime endTime }
}
```

- **scheduleEvent** (single-calendar create) — ✅ Ready, **provider-scoped token only** (was 🔶 as `createCalendarEvent`)

  The v2 proposal `createCalendarEvent` was **dropped**: org-wide tokens are rejected for event creation. The shipped mutation is `scheduleEvent`, which requires a **provider-scoped** token (`CALENDAR_EVENT` in `PROVIDER_SCOPED_RESOURCES`) and refuses to act on calendars outside the owner's scope. It returns the event directly (no `success`/`errorMessage` envelope) and raises a `GraphQLError` on failure.

```graphql
mutation ScheduleEvent($input: ScheduleEventInput!) {
  # input: { organizationId, calendarId, startTime, endTime, timezone, title, description,
  #          attendeeUserIds: [Int!], externalAttendees: [{ email, name }], rruleString }
  scheduleEvent(input: $input) { id title startTime endTime }
}
```

- **createCalendarGroupEvent** — ✅ Ready

```graphql
mutation CreateCalendarGroupEvent($input: CalendarGroupEventInput!) {
  # input: { organizationId, groupId, title, description, startTime, endTime, timezone,
  #          slotSelections: [{ slotId, calendarIds }], attendances: [{ userId }],
  #          externalAttendances: [{ externalAttendee: { email, name } }] }
  createCalendarGroupEvent(input: $input) { success errorMessage event { id title startTime endTime } }
}
```

### Booking Link Creation

All six mint mutations + `revokeBookingCode` are ✅ **Ready** (were ❌). They are org-token-gated (resource `CALENDAR_BOOKING_CODE`) and return the plaintext `code` once plus an opaque `id` for later revocation. Reschedule/cancel codes bind to a specific `eventId`.

- **createCalendarBookingCode** — ✅ Ready

```graphql
mutation CreateCalendarBookingCode($input: CreateBookingCodeInput!) {
  # input: { organizationId, calendarId, expiresAt }
  createCalendarBookingCode(input: $input) { success code id errorCode errorMessage }
}
```

- **createCalendarGroupBookingCode** — ✅ Ready

```graphql
mutation CreateCalendarGroupBookingCode($input: CreateGroupBookingCodeInput!) {
  # input: { organizationId, calendarGroupId, expiresAt }
  createCalendarGroupBookingCode(input: $input) { success code id errorCode errorMessage }
}
```

- **createCalendarRescheduleBookingCode** — ✅ Ready (bound to one event)

```graphql
mutation CreateCalendarRescheduleBookingCode($input: CreateEventCodeInput!) {
  # input: { organizationId, calendarId, eventId, expiresAt }
  createCalendarRescheduleBookingCode(input: $input) { success code id errorCode errorMessage }
}
```

- **createCalendarGroupRescheduleBookingCode** — ✅ Ready

```graphql
mutation CreateCalendarGroupRescheduleBookingCode($input: CreateGroupEventCodeInput!) {
  # input: { organizationId, calendarGroupId, eventId, expiresAt }
  createCalendarGroupRescheduleBookingCode(input: $input) { success code id errorCode errorMessage }
}
```

- **createCalendarCancellationBookingCode** — ✅ Ready

```graphql
mutation CreateCalendarCancellationBookingCode($input: CreateEventCodeInput!) {
  # input: { organizationId, calendarId, eventId, expiresAt }
  createCalendarCancellationBookingCode(input: $input) { success code id errorCode errorMessage }
}
```

- **createCalendarGroupCancellationBookingCode** — ✅ Ready

```graphql
mutation CreateCalendarGroupCancellationBookingCode($input: CreateGroupEventCodeInput!) {
  # input: { organizationId, calendarGroupId, eventId, expiresAt }
  createCalendarGroupCancellationBookingCode(input: $input) { success code id errorCode errorMessage }
}
```

- **revokeBookingCode** — ✅ Ready (idempotent)

```graphql
mutation RevokeBookingCode($input: RevokeBookingCodeInput!) {  # { organizationId, id }
  revokeBookingCode(input: $input) { success errorCode errorMessage }
}
```

### Appointment Details

- **Get CalendarEvent** — ✅ Ready (use the `eventId` argument)

```graphql
query GetEvent($eventId: Int!) {
  calendarEvents(eventId: $eventId) {
    id title description startTime endTime
    calendar { id name }
    attendeeMemberships { userId organizationId role }
    externalAttendees { id email name }
    resources { id name }
    calendarGroup { id name }   # null for provider-scoped tokens (cross-owner leak guard)
  }
}
```

- **Export .ics** — ✅ Ready (new in v3)

```graphql
query EventIcs($eventId: Int!) { eventIcs(eventId: $eventId) }   # returns RFC-5545 text, or null if not visible
```

### Reschedule / Cancel Modal (provider side)

- **List resources / calendar / user / group available times** — ✅ Ready (same as Create Appointment Modal).
- **rescheduleCalendarEvent()** — ✅ Ready (was ❌)

  Owner-scope guarded (resource `CALENDAR_EVENT`): an owner-scoped token may only reschedule events on its owner's calendars; an org-wide token acts org-wide. Supports whole-event, series-with-new-rule, and single-occurrence modes. Omit `rruleString` to preserve the existing series rule; set `recurrenceId` (the occurrence's original start) to move just one occurrence. Returns the event directly.

```graphql
mutation RescheduleCalendarEvent($input: RescheduleCalendarEventInput!) {
  # input: { organizationId, calendarId, eventId, startTime, endTime, timezone, rruleString, recurrenceId }
  rescheduleCalendarEvent(input: $input) { id title startTime endTime }
}
```

- **rescheduleCalendarGroupEvent()** — ✅ Ready (was ❌; whole-event only, no recurrence in v1)

  Moves the primary event and all linked non-primary `BlockedTime` rows together. Owner-scoped tokens may only act when the **primary** calendar is theirs.

```graphql
mutation RescheduleCalendarGroupEvent($input: RescheduleCalendarGroupEventInput!) {
  # input: { organizationId, eventId, startTime, endTime, timezone }
  rescheduleCalendarGroupEvent(input: $input) { id title startTime endTime }
}
```

- **cancelEvent()** — ✅ Ready (was ❌)

  Owner-scope guarded. Three modes via input flags: single-occurrence (`recurrenceId` set), whole event / master delete (`deleteSeries: false`), or whole-series delete (`deleteSeries: true`). Handles grouped events automatically when the event belongs to a group.

```graphql
mutation CancelEvent($input: CancelEventInput!) {
  # input: { organizationId, calendarId, eventId, deleteSeries, recurrenceId }
  cancelEvent(input: $input) { success }
}
```

  The patient-facing `*WithCode` reschedule/cancel mutations (Patient Portal) and the **change-request approval** flow (§4) remain the other two paths.

## Patient Portal

### Login Identification — no integration.

### Home / Dashboard — no integration.

### Booking Calendar

Patient reads are authorized by a single-use code, not a token. All five `*WithCode` read fields are ✅ **Ready** (repeatable — the code is not consumed by reads):

- **availableTimesWithCode** — ✅ Ready
- **availabilityWindowsWithCode** — ✅ Ready
- **unavailableWindowsWithCode** — ✅ Ready
- **calendarGroupBookableSlotsWithCode** — ✅ Ready
- **calendarGroupAvailabilityWithCode** — ✅ Ready

```graphql
query AvailabilityWindowsWithCode($code: String!, $start: DateTime!, $end: DateTime!) {
  availabilityWindowsWithCode(code: $code, startDatetime: $start, endDatetime: $end) {
    id startTime endTime canBookPartially
  }
}
```

> `isPrivate` gating is live: private calendars/groups/bundles require a code; public ones (`isPrivate = false`) accept codeless reads/booking.

### Booking Confirmation

- **createCalendarEventWithCode** — ✅ Ready (was ❌)

```graphql
mutation CreateCalendarEventWithCode($input: CreateEventWithCodeInput!) {
  # input: { code, title, startTime, endTime, timezone, externalAttendee: { email, name }, description }
  createCalendarEventWithCode(input: $input) { success event { id startTime endTime } errorCode errorMessage }
}
```

- **createCalendarGroupEventWithCode** — ✅ Ready (was ❌)

```graphql
mutation CreateCalendarGroupEventWithCode($input: CreateGroupEventWithCodeInput!) {
  # input: { code, title, startTime, endTime, timezone,
  #          slotSelections: [{ slotId, calendarIds }], externalAttendee: { email, name }, description }
  createCalendarGroupEventWithCode(input: $input) { success event { id startTime endTime } errorCode errorMessage }
}
```

  The code is consumed atomically on success and **not** consumed on failure (safe to retry).

### Intake Flag — no integration.

### Manage Appointment

- **List resources / calendar / user / group available times** — ✅ Ready (read side, via `*WithCode`).
- **rescheduleCalendarEventWithCode()** — ✅ Ready (was ❌)

```graphql
mutation RescheduleCalendarEventWithCode($input: RescheduleWithCodeInput!) {
  # input: { code, startTime, endTime, timezone }
  rescheduleCalendarEventWithCode(input: $input) { success event { id startTime endTime } errorCode errorMessage }
}
```

- **rescheduleCalendarGroupEventWithCode()** — ✅ Ready (was ❌; **v1 changes times only**, no slot re-selection)

```graphql
mutation RescheduleCalendarGroupEventWithCode($input: RescheduleGroupWithCodeInput!) {
  # input: { code, startTime, endTime, timezone }   # slot selections retained from original booking
  rescheduleCalendarGroupEventWithCode(input: $input) { success event { id startTime endTime } errorCode errorMessage }
}
```

- **cancelEventWithCode()** — ✅ Ready (was ❌)

```graphql
mutation CancelEventWithCode($input: CancelWithCodeInput!) {  # { code }
  cancelEventWithCode(input: $input) { success errorCode errorMessage }
}
```

### Pre-visit Questionnaire — no integration.

### Visit Day — no integration.

---

# What needs to be done (gap summary)

**The integration surface is now fully covered.** Every screen call in this document maps to a shipped Public API field. No 🔶/❌ API gaps remain.

What's left is integration-side decisions, not Vinta-Schedule work:

1. **Webhook payload envelope** — outgoing payloads are now `{ id, type, timestamp, data }`. Confirm the Medplum bot subscribers parse the envelope (breaking change vs. pre-envelope `calendar_event_*`).
2. **External-event change-request UX** — decide whether Building Blocks surfaces the `change_request` queue to providers or sets the org policy to `allow`/`forbidden` and skips it.
3. **Group reschedule scope** — `rescheduleCalendarGroupEventWithCode` (patient) and `rescheduleCalendarGroupEvent` (provider) both change **times only** in v1; slot re-selection is deferred. Confirm that matches the Building Blocks reschedule UX, or schedule the slot-re-selection follow-up.

Separately, one **additive enhancement** is specced but not yet built — single-calendar/bundle bookable slots and booking policies. It is not a gap in the surface documented above (the booking screens already work via group slots); it's new capability. See **[Next-step prompts](#next-step-prompts)**.

### Already done since v2 (✅ — no work)

- Calendar / event / availability / blocked-time / user **read** queries, plus `userId` filter on `calendarEvents`, `owners`, `isPrivate`, and `eventIcs`.
- Resource-calendar create/disable/import + generic `createCalendar` / `updateCalendar`.
- Availability + blocked-time create/update/delete/batch mutations.
- Calendar bundles: query + create/update/disable.
- Calendar group CRUD + grouped-event creation + group availability/bookable-slots/events.
- Single-use booking codes: 6 mint mutations + `revokeBookingCode` + 5 `*WithCode` reads + 5 `*WithCode` actions.
- Per-provider scoped tokens (`createScopedSystemUser`) with read **and** write owner-scope enforcement; `scheduleEvent` for owner-scoped event creation.
- `organization_member_created` outgoing webhook + GraphQL webhook-config management.
- External-event change-request flow (query + approve/reject).
- Org-wide admin token creation (REST + GraphQL) + token check / delete mutations.
- Whitelabel/reseller surface (createOrganization, createInvitation, createSystemUserToken, updateBranding, childOrganizations, brandingForTenant, validateReturnUrl).
- Incoming Google-provider webhook subscription management (GraphQL).

---

# Next-step prompts

All v2 implementation prompts have shipped. The integration surface described in the screens above is fully covered, and the open items in **[What needs to be done](#what-needs-to-be-done-gap-summary)** are integration-side decisions (webhook envelope parsing, change-request UX, group-reschedule scope), not new API work.

The one piece of **net-new, additive** API work on the roadmap is the bookable-slots / booking-policy feature below.

### Planned — Bookable slots for single calendars & bundles, with booking policies

> **Spec:** [`ai-plans/2026-06-26-BOOKABLE_SLOTS_SINGLE_CALENDAR_AND_BOOKING_POLICY_SPEC.md`](../ai-plans/2026-06-26-BOOKABLE_SLOTS_SINGLE_CALENDAR_AND_BOOKING_POLICY_SPEC.md) — status: **spec written, not yet planned/implemented.**

Today, discretized bookable slots are only available for a **calendar group** (`calendarGroupBookableSlots` / `calendarGroupBookableSlotsWithCode`). This feature closes two gaps that affect the **Create Appointment Modal** (provider) and the **Patient Portal → Booking Calendar** screens:

1. **Unified single-calendar + bundle bookable-slots query** (and its `_with_code` variant) — ❌ Missing. A consumer can request discretized slots for one calendar id, or for a bundle calendar id (auto-expanded; a slot is offered only when every participating child is free). Removes the workaround of wrapping a single calendar in a throwaway group.
2. **BookingPolicy model + full CRUD** on public GraphQL **and** private REST — ❌ Missing. Three guardrails — **lead time**, **max horizon**, **buffer-before/after** — resolved per target with deterministic precedence (Calendar → owning membership → org default; bundles/groups use an explicit override, else the most-restrictive combination of participants). Policies are honored on **all** slot surfaces (single, bundle, **and** the existing group query) **and** enforced on the booking write path, so discovery and booking agree.

> Backward-compatibility guarantee from the spec: with **no** policy set anywhere, the existing `calendarGroupBookableSlots` output is byte-for-byte identical to today — so this is additive, not breaking, until an org opts in by creating a policy.

Suggested driver prompt for `plan-feature`:

> Plan the feature in `ai-plans/2026-06-26-BOOKABLE_SLOTS_SINGLE_CALENDAR_AND_BOOKING_POLICY_SPEC.md`: a unified single-calendar/bundle bookable-slots query (authenticated + `_with_code`), a `BookingPolicy` model (lead time / max horizon / buffer-before / buffer-after) with a deterministic resolution chain and most-restrictive bundle/group combination, public-GraphQL + private-REST policy CRUD with organization-scope enforcement, and booking-time enforcement on the existing write path. Resolve the spec's Open Questions (owning-membership resolution, bundle-child bookability predicate, single org-default policy, delete-absent semantics, code-gated response shape) before drafting phases. Honor the negative scope: no new booking mechanics, no absolute horizon cutoff, no recurring/per-appointment-type policies, no REST slot-read surface, no changes to continuous availability/unavailability queries.
