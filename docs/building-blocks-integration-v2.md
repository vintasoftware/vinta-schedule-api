# BuildingBlocks \<-\> Vinta-Schedule Integration Project

The objective of this document is to explain how the integration is going to work and what parts are still missing in the Vinta-Schedule APIs to make sure the integration is covered.

## Status legend

Each API call below is tagged with one of:

| Tag | Meaning |
| :---- | :---- |
| ✅ **Ready** | Query/mutation already exists in the Public GraphQL API. |
| 🔶 **Service ready / GraphQL missing** | Business logic exists in a service, but there is **no** Public GraphQL field exposing it — only a thin GraphQL wrapper \+ permission mapping is needed. |
| ❌ **Missing** | Neither GraphQL nor service support exists; net-new work. |

### Schema conventions (important)

- The Public API uses **plain lists with `offset`/`limit` pagination**, *not* Relay `connection { nodes { ... } }` envelopes. The `nodes { ... }` syntax used in v1 of this doc does **not** match the real schema — examples below are rewritten to the real shape.  
- Strawberry auto-camelCases fields, so Python `start_datetime` is `startDatetime` in GraphQL.  
- Every Public API field is guarded by `IsAuthenticated` \+ `OrganizationResourceAccess`. Adding a new field **also** requires adding it to `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` (`public_api/permissions.py`) and, for genuinely new resource categories, adding a value to `PublicAPIResources` (`public_api/constants.py`).  
- The current auth model is **org-wide \+ resource-type-scoped only**. There is no per-user / per-object scoping (see §3.2 / §3.3 gaps).

---

# Integration setup questions

## 1\. How are admins going to connect to vinta-schedule?

### 1.1. create an account in vinta-schedule — ✅ Ready

Sign up and create an organization using email/password or social login (Google).

### 1.2. configure the service accounts — ✅ Ready (admin UI / existing flows)

Service accounts are necessary to sync room/resource calendars with Google Calendar and do some admin operations on Google Calendar integration.

### 1.3. \[optional\] import resource calendars — 🔶 Service ready / GraphQL missing

Resource import from Google Workspace exists as a service (`CalendarSyncService.request_organization_calendar_resources_import` / `import_organization_calendar_resources`) but is **not** exposed through the Public GraphQL API. If admins must trigger it from the integration, a mutation is needed (see Location Page).

## 2\. How do admins invite team members to vinta-schedule?

### 2.1. Automatically link vinta-schedule users with Medplum providers — ❌ Missing

Admins need to create a webhook for the "user created" event in vinta-schedule to notify a medplum bot about the new user, so it can link the identifier with their provider.

#### Observation

The **"user created" webhook event doesn't exist yet.** Outgoing webhooks *do* exist (`webhooks/` app — `WebhookConfiguration` \+ `WebhookEvent`, managed over **REST** at `/webhooks/` and `/webhook-events/`), but the only event types defined (`webhooks/constants.py`) are calendar-event / attendee events: `calendar_event_created`, `calendar_event_updated`, `calendar_event_deleted`, `calendar_event_attendee_added/removed/updated`. **To do:** add a `user_created` event type and emit it on user creation.

### 2.2. invite the user on vinta-schedule — ✅ Ready (existing invite flow)

Providers need to be invited to Vinta Schedule so they authorize it to look at their calendars in Google Calendar.

### 2.3. user accepts the invitation — ✅ Ready (will trigger the new webhook once 2.1 ships)

Users will create their account in the Vinta Schedule and that will trigger the "user created" webhook.

### 2.4. user id is linked to the medplum provider — ❌ Missing (depends on 2.1 \+ 3.2)

A Medplum bot will be triggered on user creation and that bot needs to add a vinta-schedule identifier to the Provider (so they are linked) and generate a Public API token to give the provider ability to make the necessary queries and mutations to Vinta Schedule.

### 2.5. user configures their calendars — ✅ mostly Ready

They'll be able to configure their default calendar, which calendars are listed and which sync automatically. Backing concepts already exist: `Calendar.visibility` (`ACTIVE`/`UNLISTED`/`INACTIVE`), `Calendar.sync_enabled`, `accepts_public_scheduling`, and `CalendarOwnership.is_default` (`CalendarService.get_default_calendar_for_user`). Exposure of the *mutations* to change these from the integration may still be needed.

## 3\. How does the admin integrate medplum with vinta-schedule?

### 3.1. Create an admin Public API token — ✅ Ready (REST, not GraphQL)

Tokens are created via **REST** `POST /public-api-tokens/` (`integration_name` \+ `available_resources: [...]`), gated by `IsOrganizationAdmin`. The plaintext token is returned **once** as `{system_user.id}:{token}`. Resource grants come from `PublicAPIResources`. This token can be granted resource access (calendars, calendar groups, system users, etc.).

### 3.2. Create webhooks for generating user tokens — ❌ Missing

On "user creation", generate a new provider token that only has access to manage that provider's data (manage recurring availability, manage specific availability dates, manage blocked times, free/busy checks, list events, list blocked times, schedule events).

#### Observation

vinta-schedule **does not** have user-specific Public API tokens restricted to what one user can access. Today `SystemUser` \+ `ResourceAccess` give **org-wide, resource-type-level** access only — there is no per-user / per-owner scoping. To enable §3.2 we need:

- a per-owner scope on the token (e.g. a `SystemUser.scoped_to_user` FK or a `ResourceAccess` object-level constraint), and the permission classes (`OrganizationResourceAccess`) updated to filter querysets/mutations by that owner;  
- a way for the bot to mint that token (REST endpoint already exists for org tokens — extend it to accept an owner scope, **or** add a GraphQL mutation `createScopedSystemUser`).

### 3.3. Create Patient token — ❌ Missing (depends on 3.4 \+ visibility work)

Create a patient token that's only able to check availability and create an appointment (passing the scheduling code or not depending if the calendars/calendar-groups/calendar-bundles are restricted).

#### Observation

vinta-schedule **does not** differentiate restricted/public calendars/calendar-groups/ calendar-bundles in a single uniform way:

- `Calendar` has `visibility` \+ `accepts_public_scheduling` (closest existing knob).  
- `CalendarGroup` has **no** privacy/visibility field.  
- Bundles (modeled as `Calendar` with `calendar_type=BUNDLE`) only have `visibility`.

We need a consistent `is_private` (restricted) concept across calendars / groups / bundles, plus the single-use code mutations:

- a mutation to generate a single-use **scheduling** code,  
- a mutation to generate a single-use **rescheduling** code (one specific event),  
- a mutation to generate a single-use **cancel** code (one specific event),  
- a mutation to **create** an event passing a single-use scheduling code,  
- a mutation to **reschedule** an event passing a single-use code.

Note: the data model already has `CalendarManagementToken` \+ `CalendarManagementTokenPermission` (permissions: create, update\_attendees, update\_self\_rsvp, update\_details, cancel, reschedule) plus `CalendarPermissionService.create_attendee_token()` / `create_external_attendee_update_token()`. **The model layer for single-use codes mostly exists — the gap is GraphQL mutations to mint standalone codes and to act with a code.**

### 3.4. Implement single-use scheduling codes for Patients — ❌ Missing (GraphQL surface)

Generate appointment-type unique, single-use scheduling code so patients can schedule an appointment on restricted calendars/calendar-groups/calendar-bundles. Backed by `CalendarManagementToken`; needs the GraphQL mutations from §3.3.

## 4\. How do the events get synchronized between VintaSchedule and the Building Blocks?

When we create an appointment on the Building Blocks we also need to create a CalendarEvent on VintaSchedule so we save the CalendarEvent id in the Appointment (as an identifier).

We also need webhooks so updates in VintaSchedule are automatically cascaded into the Appointment. Outgoing webhooks already exist for `calendar_event_created/updated/deleted` and attendee changes (REST `/webhooks/`), so Medplum Bots can subscribe today for event sync. The **remaining gap** is the `user_created` event (§2.1) and, optionally, exposing webhook config management over GraphQL (currently REST-only).

---

# Integration touch-points per screen

Each call lists its **status tag** and a concrete GraphQL signature. Signatures tagged 🔶/❌ are **proposed** (to be implemented); ✅ ones reflect the current schema.

## Provider/Admin App

### Login / SSO

- **Maybe List calendars** — ✅ Ready

```
query Calendars($userId: Int, $calendarType: String, $offset: Int! = 0, $limit: Int! = 100) {
  calendars(userId: $userId, calendarType: $calendarType, offset: $offset, limit: $limit) {
    id name description email calendarType capacity visibility syncEnabled
  }
}
```

### Location Page

- **List resources (id, name, description, capacity)** — ✅ Ready (filter by type)

```
query Resources($offset: Int! = 0, $limit: Int! = 100) {
  calendars(calendarType: "resource", offset: $offset, limit: $limit) {
    id name description capacity calendarType
  }
}
```

- **createResourceCalendar(name, description, capacity)** — 🔶 Service ready / GraphQL missing Backed by `CalendarService.create_resource_calendar(name, description, capacity, manage_available_windows)`.

```
mutation CreateResourceCalendar($input: CreateResourceCalendarInput!) {
  createResourceCalendar(input: $input) {   # input: { organizationId, name, description, capacity, manageAvailableWindows }
    success errorMessage calendar { id name description capacity calendarType }
  }
}
```

- **editResourceCalendar(name, description, capacity)** — ❌ Missing (No dedicated `update_resource_calendar` service method found; needs a service method \+ mutation. Works only for manual `provider=INTERNAL` resource calendars, not Google-synced ones.)

```
mutation UpdateResourceCalendar($input: UpdateResourceCalendarInput!) {
  updateResourceCalendar(input: $input) {   # input: { organizationId, calendarId, name, description, capacity }
    success errorMessage calendar { id name description capacity }
  }
}
```

- **disableResourceCalendar(id)** — 🔶 Service ready / GraphQL missing (Backed by `Calendar.visibility = INACTIVE`; expose a mutation.)

```
mutation DisableResourceCalendar($input: DisableResourceCalendarInput!) {
  disableResourceCalendar(input: $input) {  # input: { organizationId, calendarId }
    success errorMessage
  }
}
```

- *(optional)* **importResourceCalendars** — 🔶 Service ready / GraphQL missing (Wraps `CalendarSyncService.request_organization_calendar_resources_import(start_time, end_time)`.)

### Appointment Types & Calendar Groups & Bundles (Admin)

- **List calendar groups** — ✅ Ready (but see gaps: no `isPrivate`, no `owners`) Real shape (lists, not `nodes`):

```
query CalendarGroups($offset: Int! = 0, $limit: Int! = 100) {
  calendarGroups(offset: $offset, limit: $limit) {
    id name description
    slots {
      id name requiredCount order
      calendars {
        id name
        # owners { ... }   # ❌ Missing: CalendarGraphQLType has no `owners` field yet
      }
    }
    # isPrivate            # ❌ Missing: no privacy field on CalendarGroup
  }
}
```

  **Gaps:** add `isPrivate` to `CalendarGroup` (model \+ type), and add an `owners` field to `CalendarGraphQLType` (data exists via `CalendarOwnership` / `ownerships` related name, resolving to `{ id, user { id, email, profile { firstName lastName profilePicture } } }`).


- **List calendar bundles** — ❌ Missing (GraphQL surface) Bundles exist as `Calendar` with `calendar_type=BUNDLE` \+ `ChildrenCalendarRelationship`, and `CalendarBundleService` has full CRUD, but there is **no** GraphQL query/type for them. Proposed:

```
query CalendarBundles($offset: Int! = 0, $limit: Int! = 100) {
  calendarBundles(offset: $offset, limit: $limit) {
    id name description isPrivate     # isPrivate ❌ needs field
    children { id name owners { id user { id email profile { firstName lastName profilePicture } } } }
  }
}
```

- **createCalendarGroup(name, is\_private, slots)** — ✅ Ready (⚠️ `isPrivate` not yet supported)

```
mutation CreateCalendarGroup($input: CalendarGroupInput!) {
  # input: { organizationId, name, description, slots: [{ name, calendarIds, requiredCount, description, order }] }
  # ❌ add `isPrivate` to CalendarGroupInput
  createCalendarGroup(input: $input) { success errorMessage group { id name } }
}
```

- **updateCalendarGroup(name, is\_private, slots)** — ✅ Ready (⚠️ `isPrivate` not yet supported)

```
mutation UpdateCalendarGroup($input: UpdateCalendarGroupInput!) {
  # input: { organizationId, groupId, name, description, slots: [...] }  (+ isPrivate ❌)
  updateCalendarGroup(input: $input) { success errorMessage group { id name } }
}
```

- **disableCalendarGroup(id)** — ✅ Ready (as `deleteCalendarGroup`)

```
mutation DeleteCalendarGroup($input: DeleteCalendarGroupInput!) {  # { organizationId, groupId }
  deleteCalendarGroup(input: $input) { success errorMessage }
}
```

- **List calendars, filter by user** — ✅ Ready

```
query CalendarsByUser($userId: Int!) { calendars(userId: $userId) { id name calendarType } }
```

- **createCalendarBundle(name, is\_private, childrenIds)** — 🔶 Service ready / GraphQL missing (Backed by `CalendarBundleService.create_bundle_calendar(name, description, child_calendars, primary_calendar)`.)

```
mutation CreateCalendarBundle($input: CreateCalendarBundleInput!) {
  # input: { organizationId, name, description, childrenIds: [Int!]!, primaryCalendarId, isPrivate }
  createCalendarBundle(input: $input) { success errorMessage bundle { id name } }
}
```

- **updateCalendarBundle(name, is\_private, childrenIds)** — 🔶 Service ready / GraphQL missing (Backed by `CalendarBundleService.update_bundle_calendar(bundle_calendar, child_calendars, primary_calendar)`.)

```
mutation UpdateCalendarBundle($input: UpdateCalendarBundleInput!) {
  # input: { organizationId, bundleId, name, description, childrenIds, primaryCalendarId, isPrivate }
  updateCalendarBundle(input: $input) { success errorMessage bundle { id name } }
}
```

- **disableCalendarBundle(id)** — 🔶 Service ready / GraphQL missing (Set bundle `Calendar.visibility = INACTIVE`.)

```
mutation DisableCalendarBundle($input: DisableCalendarBundleInput!) {  # { organizationId, bundleId }
  disableCalendarBundle(input: $input) { success errorMessage }
}
```

### Provider Availability

- **List available times** — ✅ Ready

```
query AvailableTimes($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  availableTimes(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime recurrenceRule { rruleString }
  }
}
```

- **List unavailable times** — ✅ Ready

```
query UnavailableWindows($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  unavailableWindows(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime reason
  }
}
```

- **List AvailabilityWindows** — ✅ Ready

```
query AvailabilityWindows($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  availabilityWindows(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime canBookPartially
  }
}
```

- **List BlockedTimes** — ✅ Ready

```
query BlockedTimes($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  blockedTimes(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id startTime endTime recurrenceRule { rruleString }
  }
}
```

- **createAvailabilityWindow** — 🔶 Service ready / GraphQL missing (`AvailabilityService.create_available_time(calendar, start_time, end_time, timezone, rrule_string)`.)

```
mutation CreateAvailabilityWindow($input: CreateAvailableTimeInput!) {
  # input: { organizationId, calendarId, startTime, endTime, timezone, rruleString }
  createAvailabilityWindow(input: $input) { success errorMessage availableTime { id startTime endTime } }
}
```

- **createBlockedTime** — 🔶 Service ready / GraphQL missing (`AvailabilityService.create_blocked_time(calendar, start_time, end_time, timezone, reason, rrule_string)`.)

```
mutation CreateBlockedTime($input: CreateBlockedTimeInput!) {
  # input: { organizationId, calendarId, startTime, endTime, timezone, reason, rruleString }
  createBlockedTime(input: $input) { success errorMessage blockedTime { id startTime endTime } }
}
```

- **updateAvailabilityWindow** — 🔶 Service ready / GraphQL missing (Via `AvailabilityService.batch_modify_available_times` / recurring-exception methods.)

```
mutation UpdateAvailabilityWindow($input: UpdateAvailableTimeInput!) {
  # input: { organizationId, calendarId, availableTimeId, startTime, endTime, timezone, rruleString }
  updateAvailabilityWindow(input: $input) { success errorMessage availableTime { id startTime endTime } }
}
```

- **batchUpdateAvailabilityWindows** — 🔶 Service ready / GraphQL missing (`AvailabilityService.batch_modify_available_times(calendar, operations)` — atomic create/update/delete.)

```
mutation BatchUpdateAvailabilityWindows($input: BatchAvailabilityInput!) {
  # input: { organizationId, calendarId, operations: [{ op: "create"|"update"|"delete", availableTimeId, startTime, endTime, timezone, rruleString }] }
  batchUpdateAvailabilityWindows(input: $input) { success errorMessage availableTimes { id startTime endTime } }
}
```

- **updateBlockedTime** — 🔶 Service ready / GraphQL missing

```
mutation UpdateBlockedTime($input: UpdateBlockedTimeInput!) {
  # input: { organizationId, calendarId, blockedTimeId, startTime, endTime, timezone, reason, rruleString }
  updateBlockedTime(input: $input) { success errorMessage blockedTime { id startTime endTime } }
}
```

- **deleteAvailabilityWindow** — 🔶 Service ready / GraphQL missing

```
mutation DeleteAvailabilityWindow($input: DeleteAvailableTimeInput!) {  # { organizationId, calendarId, availableTimeId, deleteSeries }
  deleteAvailabilityWindow(input: $input) { success errorMessage }
}
```

- **deleteBlockedTime** — 🔶 Service ready / GraphQL missing

```
mutation DeleteBlockedTime($input: DeleteBlockedTimeInput!) {  # { organizationId, calendarId, blockedTimeId, deleteSeries }
  deleteBlockedTime(input: $input) { success errorMessage }
}
```

### Scheduler / Calendar

- **List events (filter by user and calendar)** — ✅ Ready (filter by calendar \+ range)

```
query CalendarEvents($calendarId: Int!, $start: DateTime!, $end: DateTime!) {
  calendarEvents(calendarId: $calendarId, startDatetime: $start, endDatetime: $end) {
    id title description startTime endTime
    attendees { id email }
    resources { id name }
  }
}
```

  Note: filtering events **by user** is not a direct argument today. Resolve the user's calendars first (`calendars(userId: ...)`), then query events per calendar, **or** add a `userId` argument to `calendarEvents` (🔶 small enhancement). Matching appointments to show clinical info live on the Building Blocks side, keyed by the stored CalendarEvent id.

### Create Appointment Modal

- **List resources** — ✅ Ready (`calendars(calendarType: "resource")`).  
- **List calendar available times** — ✅ Ready (`availabilityWindows`).  
- **List user available times** — ✅ Ready (resolve user calendar → `availabilityWindows`).  
- **List calendar group available times** — ✅ Ready

```
query GroupBookableSlots($groupId: Int!, $start: DateTime!, $end: DateTime!, $durationSeconds: Int!) {
  calendarGroupBookableSlots(groupId: $groupId, searchWindowStart: $start,
    searchWindowEnd: $end, durationSeconds: $durationSeconds) { startTime endTime }
}
```

- **createCalendarEvent** — 🔶 Service ready / GraphQL missing (`CalendarEventService.create_event(calendar_id, event_data)`.)

```
mutation CreateCalendarEvent($input: CreateCalendarEventInput!) {
  # input: { organizationId, calendarId, title, description, startTime, endTime, timezone,
  #          attendances: [{ userId }], externalAttendances: [{ externalAttendee: { email, name } }],
  #          resourceAllocations: [{ calendarId }], rruleString }
  createCalendarEvent(input: $input) {
    success errorMessage event { id title startTime endTime }
  }
}
```

- **createCalendarGroupEvent** — ✅ Ready

```
mutation CreateCalendarGroupEvent($input: CalendarGroupEventInput!) {
  # input: { organizationId, groupId, title, description, startTime, endTime, timezone,
  #          slotSelections: [{ slotId, calendarIds }], attendances: [{ userId }],
  #          externalAttendances: [{ externalAttendee: { email, name } }] }
  createCalendarGroupEvent(input: $input) { success errorMessage event { id title startTime endTime } }
}
```

### Booking Link Creation

All six are ❌ Missing as GraphQL mutations, but the model layer (`CalendarManagementToken`

+ `CalendarManagementTokenPermission`) and token-creation services already exist — these mutations are thin wrappers that mint a token with the right permission set \+ scope.  
- **createCalendarBookingCode(calendar\_id)** — ❌ Missing

```
mutation CreateCalendarBookingCode($input: CreateBookingCodeInput!) {
  # input: { organizationId, calendarId, appointmentTypeId, expiresAt }
  createCalendarBookingCode(input: $input) { success errorMessage code }
}
```

- **createCalendarGroupBookingCode(calendar\_group\_id)** — ❌ Missing

```
mutation CreateCalendarGroupBookingCode($input: CreateGroupBookingCodeInput!) {
  # input: { organizationId, calendarGroupId, appointmentTypeId, expiresAt }
  createCalendarGroupBookingCode(input: $input) { success errorMessage code }
}
```

- **createCalendarRescheduleBookingCode(calendar\_id)** — ❌ Missing (Reschedule code is bound to **one specific event**, so it takes `eventId`.)

```
mutation CreateCalendarRescheduleBookingCode($input: CreateRescheduleCodeInput!) {
  # input: { organizationId, calendarId, eventId, expiresAt }
  createCalendarRescheduleBookingCode(input: $input) { success errorMessage code }
}
```

- **createCalendarGroupRescheduleBookingCode(calendar\_group\_id)** — ❌ Missing

```
mutation CreateCalendarGroupRescheduleBookingCode($input: CreateGroupRescheduleCodeInput!) {
  # input: { organizationId, calendarGroupId, eventId, expiresAt }
  createCalendarGroupRescheduleBookingCode(input: $input) { success errorMessage code }
}
```

- **createCalendarCancellationBookingCode(calendar\_id)** — ❌ Missing

```
mutation CreateCalendarCancellationBookingCode($input: CreateCancellationCodeInput!) {
  # input: { organizationId, calendarId, eventId, expiresAt }
  createCalendarCancellationBookingCode(input: $input) { success errorMessage code }
}
```

- **createCalendarGroupCancellationBookingCode(calendar\_group\_id)** — ❌ Missing

```
mutation CreateCalendarGroupCancellationBookingCode($input: CreateGroupCancellationCodeInput!) {
  # input: { organizationId, calendarGroupId, eventId, expiresAt }
  createCalendarGroupCancellationBookingCode(input: $input) { success errorMessage code }
}
```

### Appointment Details

- **Get CalendarEvent** — ✅ Ready (use the `eventId` argument)

```
query GetEvent($eventId: Int!) {
  calendarEvents(eventId: $eventId) {
    id title description startTime endTime
    calendar { id name }
    attendees { id email profile { firstName lastName } }
    externalAttendees { id email name }
    resources { id name }
    calendarGroup { id name }
  }
}
```

### Reschedule / Cancel Modal

- **List resources / calendar / user / group available times** — ✅ Ready (same as Create Appointment Modal).  
- **rescheduleCalendarEvent()** — 🔶 Service ready / GraphQL missing (`CalendarEventService.update_event(calendar_id, event_id, event_data)` with new times.)

```
mutation RescheduleCalendarEvent($input: RescheduleCalendarEventInput!) {
  # input: { organizationId, calendarId, eventId, startTime, endTime, timezone }
  rescheduleCalendarEvent(input: $input) { success errorMessage event { id startTime endTime } }
}
```

- **rescheduleCalendarGroupEvent()** — 🔶 Service ready / GraphQL missing (Group event reschedule via group service \+ `update_event` on the grouped event.)

```
mutation RescheduleCalendarGroupEvent($input: RescheduleGroupEventInput!) {
  # input: { organizationId, groupId, eventId, startTime, endTime, timezone, slotSelections: [{ slotId, calendarIds }] }
  rescheduleCalendarGroupEvent(input: $input) { success errorMessage event { id startTime endTime } }
}
```

- **cancelEvent()** — 🔶 Service ready / GraphQL missing (`CalendarEventService.delete_event(calendar_id, event_id, delete_series)`.)

```
mutation CancelEvent($input: CancelEventInput!) {
  # input: { organizationId, calendarId, eventId, deleteSeries }
  cancelEvent(input: $input) { success errorMessage }
}
```

## Patient Portal

### Login Identification — no integration.

### Home / Dashboard — no integration.

### Booking Calendar

- **List resources** — ✅ Ready (`calendars(calendarType: "resource")`).  
- **List calendar available times** — ✅ Ready (`availabilityWindows`).  
- **List user available times** — ✅ Ready (resolve user calendar → `availabilityWindows`).  
- **List calendar group available times** — ✅ Ready (`calendarGroupBookableSlots`).

⚠️ For the **patient** (restricted) path, these reads should be permitted via a scheduling code / patient-scoped token rather than a full org token. That gating is part of the §3.3/§3.4 work (restricted visibility \+ single-use codes) and does not exist yet.

### Booking Confirmation

- **createCalendarEvent** — ❌ Missing for patients (needs the *with-code* variant) Single-calendar create exists as a service (🔶), but patient booking must accept a single-use scheduling code:

```
mutation CreateCalendarEventWithCode($input: CreateEventWithCodeInput!) {
  # input: { code, title, description, startTime, endTime, timezone,
  #          externalAttendee: { email, name } }
  createCalendarEventWithCode(input: $input) { success errorMessage event { id startTime endTime } }
}
```

- **createCalendarGroupEvent** — ❌ Missing for patients (needs the *with-code* variant)

```
mutation CreateCalendarGroupEventWithCode($input: CreateGroupEventWithCodeInput!) {
  # input: { code, title, description, startTime, endTime, timezone,
  #          slotSelections: [{ slotId, calendarIds }], externalAttendee: { email, name } }
  createCalendarGroupEventWithCode(input: $input) { success errorMessage event { id startTime endTime } }
}
```

  The authenticated (provider) variants are the existing `createCalendarEvent` (🔶) / `createCalendarGroupEvent` (✅); the patient portal needs the code-bearing variants above.

### Intake Flag — no integration.

### Manage Appointment

- **List resources / calendar / user / group available times** — ✅ Ready (read side).  
- **rescheduleCalendarEventWithCode()** — ❌ Missing

```
mutation RescheduleCalendarEventWithCode($input: RescheduleWithCodeInput!) {
  # input: { code, startTime, endTime, timezone }
  rescheduleCalendarEventWithCode(input: $input) { success errorMessage event { id startTime endTime } }
}
```

- **rescheduleCalendarGroupEventWithCode()** — ❌ Missing

```
mutation RescheduleCalendarGroupEventWithCode($input: RescheduleGroupWithCodeInput!) {
  # input: { code, startTime, endTime, timezone, slotSelections: [{ slotId, calendarIds }] }
  rescheduleCalendarGroupEventWithCode(input: $input) { success errorMessage event { id startTime endTime } }
}
```

- **cancelEventWithCode()** — ❌ Missing

```
mutation CancelEventWithCode($input: CancelWithCodeInput!) {  # { code }
  cancelEventWithCode(input: $input) { success errorMessage }
}
```

### Pre-visit Questionnaire — no integration.

### Visit Day — no integration.

---

# What needs to be done (gap summary)

Grouped by effort, smallest first.

### A. Thin GraphQL wrappers over existing services (🔶 — low effort)

Service logic exists; add mutation \+ `FIELD_TO_RESOURCE_MAPPING` entry (+ new `PublicAPIResources` value where needed):

1. **Single-calendar events**: `createCalendarEvent`, `rescheduleCalendarEvent`, `cancelEvent` (`CalendarEventService.create_event` / `update_event` / `delete_event`).  
2. **Resource calendars**: `createResourceCalendar`, `disableResourceCalendar`, *(optional)* `importResourceCalendars` (`CalendarService.create_resource_calendar`, `Calendar.visibility`, `CalendarSyncService` import).  
3. **Availability / blocked times**: `createAvailabilityWindow`, `updateAvailabilityWindow`, `batchUpdateAvailabilityWindows`, `deleteAvailabilityWindow`, `createBlockedTime`, `updateBlockedTime`, `deleteBlockedTime` (`AvailabilityService.*`).  
4. **Bundles GraphQL surface**: `CalendarBundleGraphQLType`, `calendarBundles` query, `createCalendarBundle`, `updateCalendarBundle`, `disableCalendarBundle` (`CalendarBundleService.*`).  
5. **Group event reschedule**: `rescheduleCalendarGroupEvent`.

### B. Schema additions (❌ small/medium)

6. **`owners` field** on `CalendarGraphQLType` (data via `CalendarOwnership` / `ownerships`).  
7. **`isPrivate` (restricted) concept** uniformly across `Calendar`, `CalendarGroup`, and bundle calendars — new model field(s) \+ expose on types \+ accept in create/update inputs. `Calendar.accepts_public_scheduling` / `visibility` partially cover Calendar already.  
8. **`userId` argument** on `calendarEvents` query for direct per-user filtering.

### C. Net-new building blocks (❌ medium/large)

9. **Single-use booking codes (GraphQL)**: the six `create*BookingCode` / `*RescheduleBookingCode` / `*CancellationBookingCode` mutations, plus the patient *with-code* action mutations (`createCalendarEventWithCode`, `createCalendarGroupEventWithCode`, `reschedule*WithCode`, `cancelEventWithCode`). Model layer (`CalendarManagementToken` \+ permissions \+ `CalendarPermissionService`) mostly exists; build the GraphQL surface \+ the "act with a code" auth path (no org token required).  
10. **Per-user / patient-scoped Public API tokens** (§3.2/§3.3): add owner scoping to `SystemUser`/`ResourceAccess`, enforce it in `OrganizationResourceAccess`, and provide a way for a bot to mint such tokens (extend REST `/public-api-tokens/` or add a GraphQL `createScopedSystemUser` mutation).  
11. **`user_created` outgoing webhook** (§2.1): add the event type to `webhooks/constants.py` and emit it on user creation; optionally expose webhook config management over GraphQL (currently REST-only at `/webhooks/`).

### Already done (✅ — no work)

- Calendar / event / availability / blocked-time / user **read** queries.  
- Calendar group CRUD \+ grouped-event creation \+ group availability/bookable-slots/events.  
- Org-wide admin Public API token creation (REST) \+ token check / delete mutations.  
- Outgoing webhooks for `calendar_event_*` and attendee changes (REST), usable for event sync.  
- Incoming Google-provider webhook subscription management (GraphQL).

---

# Next-step prompts

Copy/paste prompts to drive the spec & planning skills. The `create-spec` and `plan-feature`
skills both interview you before drafting, so each prompt is a self-contained brief — answer
any follow-up questions the skill asks.

## `create-spec` prompts — "Net-new building blocks" (section C)

### Prompt C-9 — Single-use booking / scheduling codes (GraphQL surface)

> Write a spec for adding **single-use scheduling codes** to the Vinta-Schedule Public
> GraphQL API so external integrators (Medplum / Building Blocks patient portal) can let
> unauthenticated patients book, reschedule, and cancel appointments on restricted
> calendars, calendar-groups, and calendar-bundles.
>
> Context: the data model mostly exists — `CalendarManagementToken` +
> `CalendarManagementTokenPermission` (permissions: create, update_attendees,
> update_self_rsvp, update_details, cancel, reschedule) and
> `CalendarPermissionService.create_attendee_token()` /
> `create_external_attendee_update_token()`. The gap is (a) GraphQL mutations to **mint
> standalone codes**, and (b) GraphQL mutations to **act with a code** without an org token.
>
> Scope the following mutations (see `docs/building-blocks-integration-v2.md` for proposed
> signatures): `createCalendarBookingCode`, `createCalendarGroupBookingCode`,
> `createCalendarRescheduleBookingCode`, `createCalendarGroupRescheduleBookingCode`,
> `createCalendarCancellationBookingCode`, `createCalendarGroupCancellationBookingCode`
> (admin/provider-minted, org-token-gated); and the code-bearing patient actions
> `createCalendarEventWithCode`, `createCalendarGroupEventWithCode`,
> `rescheduleCalendarEventWithCode`, `rescheduleCalendarGroupEventWithCode`,
> `cancelEventWithCode`.
>
> Decide and document: code lifecycle (single-use, expiry, revocation, what "used" means);
> how a code binds to an appointment type vs a specific event; the auth path for codeless
> patient calls (a code must authorize an action without `IsAuthenticated` /
> `OrganizationResourceAccess`); how codes interact with restricted-visibility booking — note
> that a separate effort introduces a uniform `is_private` (restricted) flag across
> `Calendar`, `CalendarGroup`, and bundle calendars, and codes are the mechanism that lets
> patients book on restricted ones, so design the code flow assuming that flag will exist;
> abuse/rate-limiting; and the success/error result shape. Negative scope: do not build the
> patient portal UI; do not build the `is_private` field/flag itself (that is a separate,
> independently-planned change).

### Prompt C-10 — Per-user / patient-scoped Public API tokens

> Write a spec for **per-owner-scoped Public API tokens** in Vinta-Schedule so a Medplum bot
> can mint, on user creation, a token that only manages **one specific provider's** data
> (their recurring availability, specific availability dates, blocked times, free/busy checks,
> list events, list blocked times, schedule events) — and, separately, a **patient** token
> able only to check availability and create appointments.
>
> Context: today `SystemUser` + `ResourceAccess` (`public_api/models.py`) grant **org-wide,
> resource-type-level** access only — no per-user / per-object scoping. The permission
> classes `IsAuthenticated` + `OrganizationResourceAccess` (`public_api/permissions.py`) map
> a GraphQL field to a `PublicAPIResources` value and check the system user has that resource.
>
> Decide and document: the data-model change (e.g. a `SystemUser.scoped_to_user` FK vs an
> object-level constraint on `ResourceAccess`); how `OrganizationResourceAccess` must filter
> querysets AND guard mutations by that owner so a scoped token cannot read/write other users'
> data; how the bot mints such a token (extend REST `POST /public-api-tokens/` with an owner
> scope vs a new GraphQL `createScopedSystemUser` mutation); how the patient-token variant
> differs (very narrow resource set + likely tied to a single-use scheduling-code flow that is
> being designed separately); migration / backward-compat for existing org-wide tokens; and the
> security review of the scope-bypass surface. Negative scope: do not build the single-use
> scheduling/booking codes, and do not build the `user_created` outgoing webhook — both are
> separate, independently-planned changes.

### Prompt C-11 — `user_created` outgoing webhook event

> Write a spec for adding a **`user_created` outgoing webhook event** to Vinta-Schedule so a
> Medplum bot is notified when a user is created and can link the vinta-schedule identifier to
> its Provider and mint a scoped token.
>
> Context: outgoing webhooks already exist (`webhooks/` app — `WebhookConfiguration` +
> `WebhookEvent`, managed over REST at `/webhooks/` and `/webhook-events/`, async delivery
> with exponential backoff). Current event types (`webhooks/constants.py`) are calendar-event
> and attendee events only — there is no user event.
>
> Decide and document: the new event-type constant; the trigger point on user creation (and
> whether org-membership creation vs user creation is the right signal in a multi-tenant
> model); the payload shape (what user fields a Medplum bot needs to link the provider — id,
> email, profile name, organization, identifiers); idempotency / retry semantics reusing the
> existing delivery machinery; and whether webhook-config management should also be exposed
> over GraphQL (currently REST-only) or stay REST. Negative scope: do not build per-user/
> patient-scoped tokens or single-use scheduling codes — both are separate, independently-
> planned changes that merely consume this webhook.

## `plan-feature` prompt — "Thin GraphQL wrappers" (section A, all items)

### Prompt A — Expose existing calendar services through the Public GraphQL API

> Plan a feature that adds **thin Public GraphQL wrappers over already-existing services** in
> Vinta-Schedule. Each mutation/query must follow the `create-graphql-public-query` skill
> pattern: register the field on `public_api/queries.py` / `public_api/mutations.py`, apply
> `IsAuthenticated` + `OrganizationResourceAccess`, add a `FIELD_TO_RESOURCE_MAPPING` entry in
> `public_api/permissions.py` (and a new `PublicAPIResources` value in
> `public_api/constants.py` where a new resource category is needed, e.g. calendar bundles),
> delegate to DI-injected services, and return the project's `success`/`errorMessage`/payload
> result shape. Proposed signatures live in `docs/building-blocks-integration-v2.md`.
>
> The wrappers to expose (backing service shown):
> 1. **Single-calendar events**: `createCalendarEvent`, `rescheduleCalendarEvent`,
>    `cancelEvent` — `CalendarEventService.create_event` / `update_event` / `delete_event`.
> 2. **Resource calendars**: `createResourceCalendar`, `disableResourceCalendar`, and
>    (optional) `importResourceCalendars` — `CalendarService.create_resource_calendar`,
>    `Calendar.visibility = INACTIVE`, `CalendarSyncService.request_organization_calendar_resources_import`.
> 3. **Availability / blocked times**: `createAvailabilityWindow`, `updateAvailabilityWindow`,
>    `batchUpdateAvailabilityWindows`, `deleteAvailabilityWindow`, `createBlockedTime`,
>    `updateBlockedTime`, `deleteBlockedTime` — `AvailabilityService.*` (incl.
>    `batch_modify_available_times`).
> 4. **Bundles GraphQL surface**: `CalendarBundleGraphQLType`, `calendarBundles` query,
>    `createCalendarBundle`, `updateCalendarBundle`, `disableCalendarBundle` —
>    `CalendarBundleService.*` (bundles are `Calendar` with `calendar_type=BUNDLE` +
>    `ChildrenCalendarRelationship`).
> 5. **Group event reschedule**: `rescheduleCalendarGroupEvent`.
>
> Phase the plan sensibly (e.g. by resource area), with tests per phase. Flag any service
> method that turns out NOT to exist or whose signature differs from the doc (e.g. there is no
> dedicated `update_resource_calendar` — that one is a true gap and may need a small service
> method). Negative scope (all separate, independently-planned changes — do NOT include them):
> the `is_private`/restricted flag on calendars/groups/bundles, the `owners` field on
> `CalendarGraphQLType`, the `userId` argument on the `calendarEvents` query, single-use
> scheduling/booking codes, and per-user/patient-scoped Public API tokens.

## `plan-feature` prompts — "Schema additions" (section B, one per item)

### Prompt B-6 — `owners` field on `CalendarGraphQLType`

> Plan adding an **`owners` field to `CalendarGraphQLType`** in the Public GraphQL API. The
> data already exists via `CalendarOwnership` (related name `ownerships` on `Calendar`). The
> field should resolve to a list of owners shaped
> `{ id, user { id, email, profile { firstName lastName profilePicture } } }`, so the
> Provider/Admin app can show calendar/group/bundle owners (see the calendar-groups and
> bundles queries in `docs/building-blocks-integration-v2.md`). Address N+1 / prefetching for
> nested `user.profile`, and confirm the existing `OrganizationResourceAccess` mapping for
> `calendars` already covers it (no new resource). Include tests asserting owner data is
> org-scoped and not leaked across organizations.

### Prompt B-7 — Uniform `is_private` (restricted) concept across calendars, groups, bundles

> Plan introducing a **uniform `is_private` (restricted) concept** across `Calendar`,
> `CalendarGroup`, and bundle calendars in Vinta-Schedule, plus exposing it through the Public
> GraphQL API. Today this is inconsistent: `Calendar` has `visibility`
> (`ACTIVE`/`UNLISTED`/`INACTIVE`) + `accepts_public_scheduling`; `CalendarGroup` has **no**
> privacy field; bundles (a `Calendar` with `calendar_type=BUNDLE`) only have `visibility`.
>
> Decide and document: whether `is_private` is a new field or a derived semantic over the
> existing `visibility` / `accepts_public_scheduling` knobs; the model + migration changes
> (use the `add-migration` / `migration-author` flow, multi-tenant `OrganizationModel`); how
> `is_private` is surfaced on `CalendarGraphQLType`, `CalendarGroupGraphQLType`, and the
> new bundle type, and accepted on the create/update inputs
> (`CalendarGroupInput`/`UpdateCalendarGroupInput`, and bundle inputs); and how `is_private`
> gates public vs scheduling-code-required booking. Note this flag is a prerequisite for a
> separately-planned single-use scheduling-codes feature (codes let patients book on private
> calendars/groups/bundles) — call out that downstream dependency so the field's semantics
> support it. Phase with tests covering private/public booking behavior. Negative scope: do not
> build the scheduling-code mutations themselves (separate change).

### Prompt B-8 — `userId` argument on the `calendarEvents` query

> Plan adding a **`userId` argument to the `calendarEvents` query** in `public_api/queries.py`
> so the Scheduler/Calendar screen can list a single provider's events directly instead of
> resolving the user's calendars first and querying per calendar. The filter should match
> events on calendars owned by that user (via `CalendarOwnership`) within the existing
> `startDatetime`/`endDatetime` range, stay org-scoped under `OrganizationResourceAccess`, and
> compose with the existing `calendarId` filter. Confirm interaction with the
> `get_calendar_events_expanded` service path (recurring expansion) and add tests that a user
> filter returns only that user's events and respects organization boundaries.
