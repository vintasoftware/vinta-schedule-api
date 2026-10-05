# Resource Calendar Provider Sync — Implementation Plan

Spec: [2026-10-04-RESOURCE_CALENDAR_PROVIDER_SYNC_SPEC.md](2026-10-04-RESOURCE_CALENDAR_PROVIDER_SYNC_SPEC.md). Requirements come from the spec. This plan turns them into phases and resolves the spec's **Open questions** where the Step-0 interview answered them (see **Guiding Decisions**).

**Prerequisites outside this plan (both on main):**
- **Room-allocation delete in `update_event`.** It used to delete `ResourceAllocation` rows filtered by calendar only, without filtering by event. [vintasoftware/vinta-schedule-api#372](https://github.com/vintasoftware/vinta-schedule-api/pull/372) scopes it to the updated event. Phase 12b relies on this.
- **Google room calendar id.** Calendar API calls for rooms used to pass the Directory `resourceId` where Google expects the room email. [vintasoftware/vinta-schedule-api#373](https://github.com/vintasoftware/vinta-schedule-api/pull/373) addresses rooms by resource email, and [vintasoftware/vinta-schedule-api#375](https://github.com/vintasoftware/vinta-schedule-api/pull/375) keeps Google room webhooks on the room row. Phase 8 relies on this.

## 1. Goals

1. An organization's integration partner, tenant admins (org admins only) and Vinta ops can **create, edit and delete** meeting rooms in Google Workspace or Microsoft 365 from Vinta Schedule. Writes are accepted right away and pushed to the provider in the background.
2. A **room sync lifecycle**: pending creation → synced → pending update → pending deletion → archived, plus sync failed. Failures are retried with backoff for up to 6 hours, then surfaced to org admins by email and to Vinta ops in Sentry.
3. An **hourly resync** that imports provider-side room and location changes. The provider wins only for fields it changed since the last successful sync. Rooms deleted on the provider side are archived, and their bookings are flagged.
4. A **delete flow** that previews a room's future bookings and resolves each one: cancel the deletion, move the booking to another room, remove the room from the event, or cancel the whole event. Recurring series are resolved "from now on".
5. **Microsoft room event sync** using an organization-wide (app-only) connection: delta queries plus webhook subscriptions. Google rooms get event sync as soon as Vinta Schedule creates them.

**Non-goals:**
- Outbound partner webhooks for sync events. Partners read the sync status from the room.
- Creating or editing buildings and floors. Vinta Schedule only lists them, synced locally.
- Equipment, desks, workspaces, and Google "other" resources. Rooms only.
- Publishing an existing manual (`provider=INTERNAL`) room to a provider. Manual rooms keep today's create/edit/disable behavior byte for byte.
- Moving a room between providers, moving bookings across providers, and un-deleting an archived room.
- Apple and ICS calendars.
- Room metadata beyond name, description, capacity and location.
- Writing through an acting user's OAuth token. Writes go only through the organization-level connection.
- Changing the existing on-demand resource import (`importResourceCalendars` / `request_rooms_sync`) or its free-room filter.
- Fixing the allocation-delete bug or the Google room calendar-id issue. Both are already fixed on main (see the prerequisites above **Goals**).
- Adding columns to `Calendar`. All lifecycle state lives in a side table.
- Regenerating or committing `schema.yml`. It is a generated, untracked artifact.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Sync model** | Two-way. Vinta Schedule writes push asynchronously; provider changes come back through an hourly resync. The **provider wins for any field it changed since the last successful sync**. A queued Vinta Schedule edit to such a field is dropped, the audit trail records it, and org admins get an email. Queued edits to other fields still push. *Why:* the provider is the system of record for room data, and an outage must not wipe edits that are still queued (spec **Decisions → State transitions & edge cases**). |
| **Storage shape** | A one-to-one **side table** `ResourceCalendarProviderLink`, plus `ResourceLocation` and `ResourceCalendarCreateRequest`. **No new `Calendar` columns.** *Why:* `Calendar` is joined on almost every hot query; manual rooms and every existing read stay untouched. |
| **Soft delete** | "Archived" means `Calendar.visibility = INACTIVE` (today's soft delete) plus `link.archived_at`. *Why:* the `resource_calendars` usage counter (`live_of_type` in [querysets.py:253](../calendar_integration/querysets.py#L253)) already excludes `INACTIVE`, so archiving frees the plan slot with no billing change. |
| **All `calendar_integration` schema in one phase** | Phase 2 owns every model and field this plan adds to `calendar_integration`, including the Microsoft connection model and the Google write fields. *Why:* parallel phases each adding a migration to one app produce conflicting migration leaves at merge. |
| **Adapter contract first** | Phase 2 declares the `ResourceDirectoryAdapter` and `ResourceDirectoryAdapterResolver` protocols. The push engine (Phase 7), resync (Phase 9) and busy check (Phase 12a) code against them using fakes. Phase 8 wires in the real Google and Microsoft adapters. *Why:* the engines don't wait on provider work, which keeps the graph wide. |
| **Push transport** | A Celery task per link (`push_room_to_provider_task`) that locks the link row with `select_for_update`. Transient failures re-enqueue with a countdown: exponential, starting at 1 minute, capped at 30 minutes. After `retry_deadline` (enqueue time + **6h**) the link goes to **sync failed**. Errors classified as invalid input (provider 400/422) go to sync failed immediately. Permission errors are retried, because an IT admin can restore the permission. *Why:* the 6h retry window answered spec open question 3. There is no `autoretry_for` precedent in the repo, so the retry is explicit and testable. `CELERY_TASK_ACKS_LATE=True` means every step must be idempotent. |
| **Idempotent provider create** | Google: the client sets `resourceId` deterministically to `vinta-<link uuid>`, so a replayed insert hits a 409 and is treated as success. Microsoft: the room gets a `vinta-link-<uuid>` tag, and before any POST the client looks for an existing place with that tag. *Why:* with acks-late, a crash after the provider call but before commit would otherwise create a duplicate room. |
| **Request idempotency key** | `ResourceCalendarCreateRequest` with a unique `(organization, idempotency_key)` and a 24h `expires_at`. Reusing a key with the same payload returns the original room; a different payload is rejected. A daily beat task deletes expired rows. *Why:* spec open question 5. The repo has no idempotency precedent. |
| **Concurrency** | Push and resync both lock the link row. Resync uses `skip_locked`, so a link being pushed is picked up next hour. Between Vinta Schedule actors, last write wins (spec). |
| **Plan limit** | Checked and counted when a create is accepted, using the same `check_limit(..., lock=True)` as `create_resource_calendar`. A failed create keeps counting until the room is deleted. Archiving releases the slot. Resync imports of new provider rooms use the existing headroom cap, so a partial import at the limit works as it does today. *Why:* spec open question 4. |
| **Bookability** | A room is bookable only when its link is `SYNCED`, `PENDING_UPDATE`, or `SYNC_FAILED` on an update. A guard in event create/update rejects allocations to rooms that are pending creation, failed on create, pending deletion, or archived. Rooms with no link (manual or flag-off) are unaffected. |
| **Busy check (move target)** | Vinta data plus live provider data. A room is busy if any of these overlap the slot: an event on the room's calendar, an active `ResourceAllocation` on any event (recurring events expanded), or provider free/busy (Google `freebusy` through the service account; Microsoft `getSchedule` app-only). *Why:* today's availability ignores allocations, and Microsoft room events aren't synced yet. |
| **Delete apply semantics** | Validation is all or nothing. If any booking's resolution is invalid, or the booking set changed since the preview (checked by fingerprint), nothing changes. Applying is **per booking, in order, each in its own transaction**, because every step calls the provider. If a booking fails mid-way, the engine stops, reports what was applied and what wasn't, and the room is **not** deleted. The caller previews again and retries; bookings already moved no longer reference the room, so the retry is idempotent. *Why:* provider calls can't be rolled back, so an all-or-nothing apply would be a fiction. |
| **Recurring series** | A series that started before now is resolved "from now on" through `modify_recurring_event_from_date` / `cancel_recurring_event_from_date`. Phase 12b extends `create_recurring_event_bulk_modification` so the continuation can carry a different room list; today it copies the parent's ([calendar_event_service.py:2636](../calendar_integration/services/calendar_event_service.py#L2636)). A series starting in the future is resolved as a whole. |
| **Credentials** | Google: the existing org-level `GoogleCalendarServiceAccount` gets a **second, write-scope** admin client (`admin.directory.resource.calendar`). It is built only when `write_enabled` is set, so customers who granted only the read-only scope keep working. Microsoft: a new `MicrosoftOrganizationConnection`. A tenant admin grants admin consent to Vinta's multi-tenant Entra app; Vinta stores the tenant id and gets app-only tokens through the client-credentials flow with plain HTTP (no new dependency). The app needs `Place.ReadWrite.All` and `Calendars.Read`; Exchange RBAC is a documented manual customer step. *Why:* spec open question 9. Partner tokens have no user behind them. |
| **Write-enabled check** | Google: building the write client and calling `buildings.list` must succeed. Microsoft: the token's `roles` claim must include both permissions, and `GET /places/microsoft.graph.building` must succeed. Only then is `write_enabled` set. A missing Exchange RBAC role surfaces later as a sync failure with a clear message. |
| **Locations** | Synced **locally** into `ResourceLocation` by the hourly resync and read from there by callers. Google: building plus `floorName`. Microsoft: building plus the floor (or section) place. *Why:* answered in Step 0. Reads stay fast with no provider round-trip. |
| **Microsoft room event sync** | For every synced Microsoft room in a write-enabled, flag-on org, whether imported or created: an app-only `calendarView` delta sync through the existing `CalendarSync` / sync-token pattern, triggered by Graph change-notification subscriptions. The subscriptions are stored in `CalendarWebhookSubscription` and renewed by a beat task before they expire. |
| **Surfaces** | Public GraphQL (partners), internal REST (web app, org admins only through `IsOrganizationAdmin`), and Django admin (ops). `createResourceCalendar` and the REST create get an optional `provider` input that defaults to `INTERNAL`; omitting it gives today's behavior. |
| **Public API grants** | Create and update reuse `create_resource_calendar` / `update_resource_calendar`. New grants, one per action: `list_resource_locations`, `retry_resource_calendar_sync`, `preview_resource_calendar_deletion`, `delete_resource_calendar`, `resolve_flagged_resource_bookings`. All are org-wide only and **not** added to `PROVIDER_SCOPED_RESOURCES`. *Why:* spec open question 7. |
| **Notifications** | Email through vintasend to org admins: active memberships holding `MANAGE_MEMBERS`, the same set `_notify_eligible_approvers` uses in [external_event_change_request_service.py:281](../calendar_integration/services/external_event_change_request_service.py#L281). The types are sync failed, edit discarded, and bookings flagged. Organizers get an email when a booking is moved or loses its room. *Why:* spec open question 10. |
| **Feature flag** | Key `resource_calendar_provider_sync`, **per organization**, default **off**. It lives in a new `common/feature_flags.py` and is stored in an `OrganizationFeatureFlag` row that ops toggle in Django admin. It gates every new endpoint, the `provider` input, synced-room edit/delete/retry, the resync beat (only flag-on orgs), Microsoft event sync, and the consent and verify endpoints. When it is off, every existing path behaves as before, byte for byte. **Flip-on criterion:** Phase 15 is merged, the partner's staging end-to-end test (spec **Objectives**) passes on both providers, and one pilot org has run 48h on staging with no sync incident. **Removal:** Phase 16, after 2 weeks at 100% in production. *Why:* there was no flag module to reuse, and a database toggle flips without a deploy. |
| **Spike** | Phase 0 ships a runnable spike script and a findings doc for spec open questions 1, 2 and 8. Humans run it against sandbox tenants. Adapter phases code to the documented APIs. If a finding contradicts them, `amend-plan` adjusts Phase 4 or Phase 6. |

## 3. Data Model Changes

All additions are in Phase 2 (calendar_integration) and Phase 1 (organizations). Every model is tenant-scoped (`SingleOrganizationModelMixin`, `SafeRelationNullInitMixin`, `BaseModel`, with `OrganizationScopedManager` or a `from_queryset` subclass), and foreign keys to scoped models use `OrganizationSafeForeignKey` / `OrganizationSafeOneToOneField`.

### 3.1 New `OrganizationFeatureFlag` (organizations)

```python
class OrganizationFeatureFlag(SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel):
    key = models.CharField(max_length=100)
    enabled = models.BooleanField(default=False)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["organization", "key"], name="uniq_org_feature_flag")]
```

`common/feature_flags.py`:

```python
RESOURCE_CALENDAR_PROVIDER_SYNC = "resource_calendar_provider_sync"

def is_enabled(key: str, organization_id: int) -> bool: ...
def organization_ids_with_flag(key: str) -> list[int]:  # cross-org; uses unscoped() with a comment
```

### 3.2 New constants (`calendar_integration/constants.py`)

- `ResourceSyncStatus`: `PENDING_CREATION`, `SYNCED`, `PENDING_UPDATE`, `PENDING_DELETION`, `SYNC_FAILED`, `ARCHIVED`.
- `ResourceSyncOperation`: `CREATE`, `UPDATE`, `DELETE`.

### 3.3 New `ResourceLocation`

```python
class ResourceLocation(...):
    provider = models.CharField(max_length=50, choices=CalendarProvider)
    external_building_id = models.CharField(max_length=255)
    building_name = models.CharField(max_length=255)
    external_floor_id = models.CharField(max_length=255)   # MS floor/section place id; Google floorName
    floor_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    last_seen_at = models.DateTimeField()
    # unique (organization, provider, external_building_id, external_floor_id)
```

### 3.4 New `ResourceCalendarProviderLink`

```python
class ResourceCalendarProviderLink(...):
    calendar = OrganizationSafeOneToOneField(Calendar, on_delete=models.CASCADE, related_name="provider_link")
    provider = models.CharField(max_length=50, choices=CalendarProvider)
    sync_status = models.CharField(max_length=32, choices=ResourceSyncStatus, db_index=True)
    failed_operation = models.CharField(max_length=16, choices=ResourceSyncOperation, blank=True)
    last_error = models.TextField(blank=True)
    location = OrganizationSafeForeignKey(ResourceLocation, null=True, on_delete=models.PROTECT)
    provider_snapshot = models.JSONField(default=dict)   # {name, description, capacity, location_ref} at last successful sync
    pending_fields = models.JSONField(default=dict)      # vinta edits not yet pushed
    provisional_key = models.UUIDField(default=uuid.uuid4, unique=True)  # deterministic provider id / tag
    attempt_count = models.PositiveIntegerField(default=0)
    retry_deadline = models.DateTimeField(null=True)
    last_synced_at = models.DateTimeField(null=True)
    archived_at = models.DateTimeField(null=True)
    flagged_bookings_at = models.DateTimeField(null=True)  # set when provider deleted the room with future bookings
```

Model helpers (pure, unit-tested in Phase 2): `fields_changed_by_provider(provider_values) -> set[str]`, `is_bookable` (property), `mark_pushed(fields, provider_values)`.

Queryset: `due_for_push()`, `for_resync(provider)`, `locked_for_update(link_id, skip_locked=False)`, `with_flagged_bookings()`.

### 3.5 New `ResourceCalendarCreateRequest`

```python
class ResourceCalendarCreateRequest(...):
    idempotency_key = models.CharField(max_length=255)
    request_fingerprint = models.CharField(max_length=64)   # sha256 of normalized payload
    calendar = OrganizationSafeForeignKey(Calendar, on_delete=models.CASCADE)
    expires_at = models.DateTimeField(db_index=True)
    # unique (organization, idempotency_key)
```

### 3.6 New `MicrosoftOrganizationConnection`

```python
class MicrosoftOrganizationConnection(...):
    tenant_id = models.CharField(max_length=64, blank=True)
    consent_state = models.CharField(max_length=128, blank=True)  # nonce for the admin-consent round trip
    consented_at = models.DateTimeField(null=True)
    write_enabled = models.BooleanField(default=False)
    verified_at = models.DateTimeField(null=True)
    last_verification_error = models.TextField(blank=True)
    # unique (organization)
```

No secrets are stored: app-only tokens are minted with Vinta's own `MS_CLIENT_ID` / `MS_CLIENT_SECRET` and the stored `tenant_id`.

### 3.7 `GoogleCalendarServiceAccount.write_enabled` / `.write_verified_at`

Two nullable or defaulted columns on [models.py:2225](../calendar_integration/models.py#L2225). `write_enabled = BooleanField(default=False)` and `write_verified_at = DateTimeField(null=True)`.

### 3.8 Type plumbing

- `calendar_integration/services/dataclasses.py`: `ResourceLocationData`, `RoomDirectoryData` (external_id, email, name, description, capacity, location_ref, provider_payload), `RoomWriteData` (name, description, capacity, location_ref, provisional_key), `BusyWindow`.
- `calendar_integration/services/protocols/resource_directory_adapter.py`: `ResourceDirectoryAdapter` (`list_locations`, `list_rooms`, `create_room`, `update_room`, `delete_room`, `get_free_busy`) and `ResourceDirectoryAdapterResolver` (`adapter_for(organization, provider)`, `is_write_enabled(organization, provider)`).
- `calendar_integration/exceptions.py`: `ResourceDirectoryError` (with `is_transient: bool`), `ResourceDirectoryInvalidInputError`, `ResourceDirectoryPermissionError`, `ResourceDirectoryNotFoundError`.

## 4. API Design

All new GraphQL fields use `permission_classes=[IsAuthenticated, OrganizationResourceAccess]` and follow the existing `success` / `error_message` result convention (see `CreateResourceCalendarResult`, [public_api/mutations.py:500](../public_api/mutations.py#L500)). All new REST actions are on `CalendarViewSet` ([views.py:233](../calendar_integration/views.py#L233)) with `IsOrganizationAdmin`, unless noted. If the flag is off for the organization, every new field and endpoint returns the error "Resource calendar provider sync is not enabled for this organization." The REST endpoints return 404, so the surface isn't advertised.

### 4.1 Provider connections (Phases 4, 5)

| Surface | Shape |
|---|---|
| `POST /calendar/google-service-account/verify-write-access/` | → `{write_enabled, write_verified_at, error}` |
| `POST /calendar/microsoft-connection/consent-url/` | → `{consent_url}` (signed state stored on the connection) |
| `GET /calendar/microsoft-connection/callback/` | Plain view (browser redirect from Microsoft). It checks the signed state and narrows explicitly with `filter_by_organization`, then redirects to `FRONTEND_BASE_URL` with the outcome. |
| `POST /calendar/microsoft-connection/verify/` | → `{write_enabled, verified_at, error}` |

### 4.2 Locations and create (Phase 10)

- GraphQL query `resourceLocations(provider: CalendarProvider, pagination)` → paginated `ResourceLocationGraphQLType {id, provider, buildingName, floorName}`. Grant: `list_resource_locations`.
- `CreateResourceCalendarInput` gets `provider: CalendarProvider = INTERNAL`, `locationId: ID | None`, and `idempotencyKey: String | None`. `INTERNAL` keeps today's path. `GOOGLE` / `MICROSOFT` require the flag, a write-enabled connection, and an active location from that provider.
- `CalendarGraphQLType.providerSync` → `ResourceCalendarProviderSyncGraphQLType {status, failedOperation, lastError, lastSyncedAt, location, flaggedBookingsAt}`, or `null` for rooms without a link.
- REST: `GET /calendar/resource-locations/?provider=`, and `POST /calendar/resource/` (`ResourceCalendarCreateSerializer`) gets `provider`, `location_id` and `idempotency_key`.
- Errors: invalid provider or location, provider not write-enabled, over limit (existing `raise_over_limit_graphql_error`), idempotency key reused with a different payload.

### 4.3 Edit and retry (Phase 11)

- `UpdateResourceCalendarInput` gets `locationId`. For synced rooms (flag on), the existing guard at [calendar_service.py:1198](../calendar_integration/services/calendar_service.py#L1198) is lifted, and the edit writes `Calendar` and records `pending_fields`. `manage_available_windows`, `is_private` and `visibility` stay vinta-only and are never pushed.
- New mutation `retryResourceCalendarSync(calendarId)`. Grant: `retry_resource_calendar_sync`.
- REST: `PATCH /calendar/resource/{id}/` (new dedicated action) and `POST /calendar/resource/{id}/retry-sync/`. Generic `CalendarViewSet.update` / `partial_update` / `destroy` **reject synced rooms when the flag is on** and point to the dedicated actions. That closes the plain-ORM-save path in `CalendarSerializer.update` ([serializers.py:258](../calendar_integration/serializers.py#L258)) for synced rooms.

### 4.4 Delete (Phase 12c) and flagged bookings (Phase 13)

- Query `resourceCalendarDeletionPreview(calendarId)` → `{fingerprint, bookings: [{eventId, title, start, end, isSeries, seriesFrom, organizer}]}`. Grant: `preview_resource_calendar_deletion`.
- Mutation `deleteResourceCalendar(calendarId, fingerprint, defaultResolution, overrides: [{eventId, resolution, targetCalendarId?, cancelMode?}])`. `resolution` is one of `ABORT` (cancel the deletion), `MOVE`, or `CANCEL`; `cancelMode` is `REMOVE_ROOM` or `CANCEL_EVENT`. The result is `{success, errorMessage, rejectedBookings: [{eventId, reason}], appliedBookings, pendingBookings}`. Grant: `delete_resource_calendar`.
- Mutation `resolveFlaggedResourceBookings(calendarId, fingerprint, defaultResolution, overrides)`. It uses the same shapes, for an archived room with `flaggedBookingsAt` set. Grant: `resolve_flagged_resource_bookings`.
- REST: `GET /calendar/resource/{id}/deletion-preview/`, `POST /calendar/resource/{id}/delete/`, and `POST /calendar/resource/{id}/resolve-flagged-bookings/`.

## 5. Phased Rollout

### Crew

| Agent | Role | Tier | Takes | Why this tier |
|---|---|---|---|---|
| `tier4` | implementer | 4 | Phase 7, Phase 9, Phase 12b | An acks-late retry protocol with row locks and deadline-driven state transitions; a provider-wins diff racing that protocol; a multi-step apply over provider-backed event edits, including the recurring-series continuation. No precedent in this repo. |
| `tier3-1` | implementer | 3 | Phase 2, Phase 4, Phase 12a, Phase 10, Phase 11, Phase 12c | The schema plus the contracts, then the Google adapter and the busy check, then the create → edit → delete surface chain. Keeping the chain on one agent means one session carries the public-API conventions across all three. |
| `tier3-2` | implementer | 3 | Phase 0, Phase 5, Phase 6, Phase 14a, Phase 14b | The whole Microsoft track: OAuth admin consent, app-only tokens, Places writes, delta sync, webhooks. One session holds the Graph client throughout. Phase 0 is Tier 2 work, given to this member so `tier2` isn't serialized in wave 1. |
| `tier2` | implementer | 2 | Phase 1, Phase 3, Phase 15, Phase 8, Phase 13, Phase 16 | Flag module, vintasend notification types, admin registration, DI wiring, a thin surface over an existing engine, flag deletion. All follow a pattern already in the repo. |
| `reviewer-1` | reviewer | 3 | — reviews Tier 2 and Tier 3 phases | Cheapest reviewer covering most of the plan. |
| `reviewer-2` | reviewer | 4 | — reviews Tier 4 phases and the `**Review models**` overrides (Phase 2, Phase 5, Phase 9) | Concurrency, retry and security-sensitive review. |

### Execution graph

Wave = how deep a phase sits in the dependency graph. Phases in the same wave have no dependency on each other and are implemented at the same time, up to the project's 3-lane budget.

| Wave | Phases | Agent | Depends on |
|---|---|---|---|
| 1 | Phase 0, Phase 1, Phase 2, Phase 3 | `tier3-2`, `tier2`, `tier3-1`, `tier2` | — |
| 2 | Phase 4, Phase 5, Phase 7, Phase 9, Phase 12a | `tier3-1`, `tier3-2`, `tier4`, `tier4`, `tier3-1` | Phase 1, Phase 2, Phase 3 |
| 3 | Phase 6, Phase 10, Phase 12b, Phase 15 | `tier3-2`, `tier3-1`, `tier4`, `tier2` | Phase 5, Phase 7, Phase 12a |
| 4 | Phase 8, Phase 11, Phase 14a | `tier2`, `tier3-1`, `tier3-2` | Phase 4, Phase 6, Phase 7, Phase 10 |
| 5 | Phase 12c, Phase 14b | `tier3-1`, `tier3-2` | Phase 2, Phase 7, Phase 11, Phase 12b, Phase 14a |
| 6 | Phase 13 | `tier2` | Phase 9, Phase 12b, Phase 12c |
| 7 | Phase 16 — remove the `resource_calendar_provider_sync` flag | `tier2` | every gated phase (deferred — soak-gated) |

**File overlap:**
- `di_core/containers.py` is edited by Phases 3, 4, 5, 7, 8, 9, 12a and 12b. Each adds its own provider block, so merges are trivial.
- `calendar_integration/routes.py`: Phases 4 and 5 (wave 2). Each adds a route.
- `calendar_integration/tasks/__init__.py`: Phases 7 and 9 (wave 2). Each adds exports.
- `vinta_schedule_api/celerybeat_schedule.py`: Phase 9 (wave 2), Phase 10 (wave 3) and Phase 14b (wave 5). They are in different waves.
- The public-API files (`public_api/mutations.py`, `public_api/permissions.py`, `public_api/constants.py`, `calendar_integration/serializers.py`, `calendar_integration/views.py`) are a **real** overlap between Phases 10, 11, 12c and 13. They are deliberately chained (Phase 10 → Phase 11 → Phase 12c → Phase 13) so that each builds on the one before rather than all four colliding.

**Wave width vs lanes:** waves 1–3 are wider than the 3-lane budget, so the executor queues the overflow. Within a wave, `tier4` runs Phase 7 before Phase 9, and `tier3-1` runs Phase 4 before Phase 12a.

**Idle:**
- `tier4`: wave 1, and waves 4–7.
- `tier2`: waves 2 and 5.
- `tier3-1` and `tier3-2`: waves 6–7.

The tail of the graph (waves 5–7) is the create → edit → delete → flagged-bookings surface chain, which is serial because those phases share files.

### Phase 0 — Provider capability spike script and findings doc

**Goal**: give humans a runnable script and a findings template that answer spec open questions 1, 2 and 8 against sandbox tenants. Ship value: no runtime behavior. It removes the biggest unknowns in Phases 4 and 6 before staging.

**Depends on**: nothing — starts from the base branch.

**Feature flag**: none — not importable by the app; a standalone script.

Changes:
1. `@scripts/spikes/resource_directory_spike.py`: a CLI script (argparse, `logging`, credentials from env vars, **dry-run by default**) with subcommands:
   - `google-create-delete`: insert a room with a client-chosen `resourceId`, read it back, patch capacity, delete it; also check whether a duplicate name is rejected and whether `buildingId` is required for `CONFERENCE_ROOM`.
   - `ms-create-delete`: POST a room under a floor; record the returned `emailAddress`, provisioning delay, and tag round-trip; PATCH; DELETE; then check whether the room mailbox still exists (`GET /users/{email}`).
   - `ms-description`: list the writable room properties and check whether any of them holds free text.
   - It logs only opaque ids, never room content.
2. `@docs/integrations/resource-directory-spike.md`: the questions, how to run each subcommand, and a findings table to fill in (question → observed → impact on Phase 4/6).

Spec use-case: shared scaffolding — no use-case yet (supports spec **Open questions** 1, 2, 8).

Tests:
- **Unit**: `@scripts/spikes/tests/test_resource_directory_spike.py` — argument parsing; dry-run makes no HTTP calls (requests are patched to fail if called).

**Assigned to**: `tier3-2` (Tier 3) — Tier 2 work (a CLI over two documented REST APIs), given to `tier3-2` so `tier2` doesn't serialize three phases in wave 1. It also primes the session that later builds the Microsoft track.

**Reusable skills**: `add-one-off-script` (for the dry-run-by-default, logging-safe script contract).

Acceptance: `uv run python scripts/spikes/resource_directory_spike.py --help` lists the three subcommands; running any of them without `--execute` makes zero HTTP calls.

### Phase 1 — Per-organization feature flag module

**Goal**: ops can turn named features on per organization from Django admin, and code checks them with `is_enabled(key, organization_id)`. Ship value: none on its own. It's the gate every later phase uses.

**Depends on**: nothing — starts from the base branch.

**Feature flag**: none — this phase creates the flag mechanism; nothing reads it yet.

Changes:
1. [organizations/models.py](../organizations/models.py): add `OrganizationFeatureFlag` (see **Data Model Changes**), plus its migration.
2. `@common/feature_flags.py`: the `RESOURCE_CALENDAR_PROVIDER_SYNC` constant; `is_enabled(key, organization_id)` (scoped read through `filter_by_organization`); `organization_ids_with_flag(key)` (cross-org, `unscoped()`, with a comment saying why: beat tasks iterate every org).
3. [organizations/admin.py](../organizations/admin.py): register `OrganizationFeatureFlag` with list filters by key and enabled. Use `unscoped_default_manager()` only around the organization FK form field, per CLAUDE.md.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `@common/tests/test_feature_flags.py` — missing row means off; enabled row means on; another org's row doesn't leak; `organization_ids_with_flag` returns only enabled orgs.
- **Integration**: `@organizations/tests/test_feature_flag_admin.py` — the admin changelist and change form render and save.

**Assigned to**: `tier2` (Tier 2) — one tenant-scoped model, an admin registration, and two query helpers, all with precedent.

**Reusable skills**: `add-model`, `add-migration`, `write-unit-test`.

Acceptance: an `OrganizationFeatureFlag(key="resource_calendar_provider_sync", enabled=True)` row makes `is_enabled` true for that org only.

### Phase 2 — Room provider sync schema and adapter contracts

**Goal**: every table, column, enum, dataclass, protocol and exception the rest of the plan builds on exists and is tested. Ship value: none on its own. It's the shared foundation that lets Phases 4, 5, 7, 9 and 12a run in parallel.

**Depends on**: nothing — starts from the base branch.

**Feature flag**: none — schema plus unreferenced types; nothing reachable.

Changes:
1. [calendar_integration/constants.py](../calendar_integration/constants.py): `ResourceSyncStatus`, `ResourceSyncOperation`.
2. [calendar_integration/models.py](../calendar_integration/models.py): `ResourceLocation`, `ResourceCalendarProviderLink`, `ResourceCalendarCreateRequest`, `MicrosoftOrganizationConnection`; `write_enabled` / `write_verified_at` on `GoogleCalendarServiceAccount`. Include the link's pure helpers (`fields_changed_by_provider`, `is_bookable`, `mark_pushed`).
3. [calendar_integration/querysets.py](../calendar_integration/querysets.py) and [calendar_integration/managers.py](../calendar_integration/managers.py): the queryset methods in **Data Model Changes**. Managers are built with `OrganizationScopedManager.from_queryset(...)`.
4. One migration in `calendar_integration/migrations/` for all of the above. It is additive only: new tables, plus two columns on `GoogleCalendarServiceAccount` that have defaults.
5. [calendar_integration/services/dataclasses.py](../calendar_integration/services/dataclasses.py): `ResourceLocationData`, `RoomDirectoryData`, `RoomWriteData`, `BusyWindow`.
6. `@calendar_integration/services/protocols/resource_directory_adapter.py`: `ResourceDirectoryAdapter`, `ResourceDirectoryAdapterResolver`.
7. [calendar_integration/exceptions.py](../calendar_integration/exceptions.py): `ResourceDirectoryError` family.
8. [calendar_integration/signals.py](../calendar_integration/signals.py): define the `resource_room_synced` (`calendar_id`, `provider`, `created: bool`) and `resource_room_archived` (`calendar_id`, `provider`) signals. They are defined here so the push engine (Phase 7) and the resync (Phase 9) can both send them while running in parallel.
9. [calendar_integration/factories.py](../calendar_integration/factories.py): `create_resource_location`, `create_resource_provider_link`, and `create_microsoft_organization_connection` helpers. They follow the file's helper-function style, since it has no `Calendar` factory class.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `@calendar_integration/tests/models/test_room_provider_sync_models.py`:
  - `fields_changed_by_provider` covers no change, a single-field change, and a location change.
  - `is_bookable` for every status.
  - `mark_pushed` clears only the fields whose pushed value still matches what is pending.
  - The uniqueness constraints hold.
  - Organization scoping: a link in org A is invisible from org B.

**Assigned to**: `tier3-1` (Tier 3) — five interrelated tenant-scoped models with safe relations and protocols that four later phases code against. A wrong contract here costs four phases.

**Review models**: reviewer Tier 4 — the protocol and model contracts are consumed by four parallel phases; a missing field or a mis-specified helper is expensive to unwind after wave 2 starts.

**Reusable skills**: `add-model`, `add-migration` (with the `migration-author` agent), `write-unit-test`.

Acceptance: `uv run python manage.py makemigrations --check` is clean; the new models round-trip through factories; the protocols type-check with a fake implementation in tests.

### Phase 3 — Room sync admin email notifications

**Goal**: a `RoomSyncNotifier` service can email org admins about sync failures, discarded edits and flagged bookings, and email organizers about moved or cancelled room bookings. Ship value: none until callers exist.

**Depends on**: nothing — starts from the base branch.

**Feature flag**: none — no caller until Phases 7, 9 and 12b, which are gated.

Changes:
1. `@calendar_integration/services/room_sync_notifier.py`: `RoomSyncNotifier` (stateless, DI-injected `notification_service`), with methods `notify_sync_failed(calendar_id, operation, reason)`, `notify_edit_discarded(calendar_id, fields)`, `notify_bookings_flagged(calendar_id, count)`, and `notify_booking_room_changed(event_id, organizer_user_id, change)`. Admins are active memberships holding `MANAGE_MEMBERS`, as in [external_event_change_request_service.py:281](../calendar_integration/services/external_event_change_request_service.py#L281). Every send goes through `transaction.on_commit`. Error text is kept generic: room id and name, never attendee or event content.
2. [calendar_integration/notification_contexts.py](../calendar_integration/notification_contexts.py): register four contexts.
3. `@templates/calendar_integration/emails/`: `room_sync_failed`, `room_edit_discarded`, `room_bookings_flagged` and `booking_room_changed`, each with `.subject.txt`, `.pre_header.txt` and `.body.html`, following the `templates/payments/emails/dunning_*` layout.
4. [di_core/containers.py](../di_core/containers.py): register `room_sync_notifier`.

Spec use-case: shared scaffolding — no use-case yet (serves use-cases 3, 4, 5).

Tests:
- **Unit**: `@calendar_integration/tests/services/test_room_sync_notifier.py`:
  - Admins only (a non-admin member and an inactive member get nothing).
  - Sends happen on commit, and nothing is sent when the transaction rolls back.
  - Each context renders subject and body.

**Assigned to**: `tier2` (Tier 2) — a notification service that copies an existing admin-notify pattern.

**Reusable skills**: `write-unit-test`.

Acceptance: calling each notifier method inside a committed transaction creates one email notification per org admin, with the matching template.

### Phase 4 — Google directory write access and adapter

**Goal**: an org admin can verify that the Google service account has the write scope, and the Google adapter implements every `ResourceDirectoryAdapter` method (spec use-case 0, Google).

**Depends on**: Phase 1 (`is_enabled` / `RESOURCE_CALENDAR_PROVIDER_SYNC`, which gates the verify endpoint), Phase 2 (`GoogleCalendarServiceAccount.write_enabled` / `write_verified_at`, the `ResourceDirectoryAdapter` protocol, `RoomDirectoryData` / `RoomWriteData` / `ResourceLocationData`, and the `ResourceDirectoryError` family).

**Feature flag**: `resource_calendar_provider_sync` — the verify endpoint returns 404 when the flag is off. The adapter methods are new and have no existing caller. `from_service_account(..., write=False)` stays the default, so every existing caller keeps the read-only scope.

Changes:
1. [google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py):
   - Add `_SA_WRITE_SCOPES = ["…/admin.directory.resource.calendar", "…/calendar.readonly"]` and a `write: bool = False` parameter on `from_service_account`. `_SA_SCOPES` is unchanged.
   - Implement the following, mapping Directory errors to the `ResourceDirectoryError` family (400/409/412 are invalid input, except a 409 on a replayed insert, which is treated as success; 401/403 are permission errors; 5xx and 429 are transient):
     - `list_locations` (`buildings.list` plus each building's `floorNames`)
     - `list_rooms` (every `CONFERENCE_ROOM`, with no free/busy filter, unlike `get_available_calendar_resources`)
     - `create_room` (`resources.calendars.insert` with `resourceId = vinta-<provisional_key>`)
     - `update_room` (`patch`)
     - `delete_room`
     - `get_free_busy` (Calendar `freebusy.query` on the room email)
   - Reuse `write_quote_limiter`.
2. `@calendar_integration/services/google_write_access_service.py`: `GoogleWriteAccessService.verify(organization)`. It builds the write client for the org-level service account, calls `buildings.list(maxResults=1)`, sets `write_enabled` / `write_verified_at` on success, and records a remediation message ("grant the admin.directory.resource.calendar scope to the service account in the Admin Console") on failure.
3. `@calendar_integration/google_write_access_views.py` plus [routes.py](../calendar_integration/routes.py): `POST /calendar/google-service-account/verify-write-access/`, using `IsOrganizationAdmin` and `TenantScopedViewMixin`.
4. [di_core/containers.py](../di_core/containers.py): register `google_write_access_service`.

Spec use-case: use-case 0 (IT admin enables write access) — Google.

Tests:
- **Unit**: [test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py) — each new method against a mocked `admin_client` and Calendar client: error classification, the deterministic `resourceId`, a 409 replay treated as success, building/floor flattening. `write=False` still requests only the read-only scopes (regression).
- **Integration**: `@calendar_integration/tests/services/test_google_write_access_service.py` (success and missing-scope failure); `@calendar_integration/tests/test_google_write_access_views.py`:
  - Admin only.
  - Flag off returns 404.
  - Flag on with a granted scope sets `write_enabled`.

**Assigned to**: `tier3-1` (Tier 3) — six provider calls with error classification and a scope split that must not regress existing read-only customers.

**Reusable skills**: `create-rest-endpoint`, `write-unit-test`.

Acceptance: with the flag on and the write scope granted, the verify endpoint sets `write_enabled=True`. With the flag off it returns 404, and the existing room import still builds the read-only client.

### Phase 5 — Microsoft organization connection via admin consent

**Goal**: an org admin can connect the organization's Microsoft 365 tenant through admin consent, and Vinta Schedule can mint and verify app-only tokens for it (spec use-case 0, Microsoft).

**Depends on**: Phase 1 (`is_enabled` / `RESOURCE_CALENDAR_PROVIDER_SYNC`, which gates the consent endpoints), Phase 2 (the `MicrosoftOrganizationConnection` model).

**Feature flag**: `resource_calendar_provider_sync` — the consent-url, callback and verify endpoints return 404 when the flag is off. The new `MS_CLIENT_*` settings are read only by these paths.

Changes:
1. Add the env vars `MS_CLIENT_ID` (config) and `MS_CLIENT_SECRET` (secret) through the `add-env-var` skill:
   - [settings/base.py](../vinta_schedule_api/settings/base.py) (default `""`; this also defines the settings the existing adapter already reads), `.env.example`, `.env.docker.example`.
   - The Terraform `container_environment` and the `local.secret_keys` list.
   - The CLAUDE.md env section.
2. `@calendar_integration/services/calendar_clients/ms_app_only_token.py`: `MicrosoftAppOnlyTokenProvider.get_token(tenant_id)`. It uses client credentials (`POST https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope `https://graph.microsoft.com/.default`) with plain `requests`, caches tokens in Redis until 5 minutes before expiry, and returns the decoded `roles` claim (signature not verified; used only to tell the admin which permission is missing). The secret and token never appear in logs.
3. `@calendar_integration/services/microsoft_connection_service.py`: `MicrosoftConnectionService` with:
   - `build_consent_url(organization)`: a signed state carrying the org id and a nonce, stored on the connection.
   - `complete_consent(state, tenant, admin_consent)`: validates the signature and the nonce (single use), then stores `tenant_id` and `consented_at`.
   - `verify(organization)`: token, `roles` contains `Place.ReadWrite.All` and `Calendars.Read`, then `GET /places/microsoft.graph.building?$top=1`. On success it sets `write_enabled` / `verified_at`; otherwise it sets `last_verification_error` to a remediation message, including the Exchange RBAC step.
4. `@calendar_integration/microsoft_connection_views.py` plus [routes.py](../calendar_integration/routes.py): the three endpoints in **API Design → Provider connections**. The callback is a plain `View` (no tenant binding), so it narrows explicitly with `filter_by_organization(org_id_from_signed_state)`, with a comment. It redirects only to `FRONTEND_BASE_URL`.
5. `@docs/integrations/microsoft-room-sync-setup.md`:
   - Vinta ops: register the multi-tenant Entra app (redirect URI, app permissions `Place.ReadWrite.All` and `Calendars.Read`).
   - Customer IT: the admin consent link, plus the Exchange RBAC role assignment (`TenantPlacesManagement`, `MailRecipient`).
6. [di_core/containers.py](../di_core/containers.py): register the two services.

Spec use-case: use-case 0 (IT admin enables write access) — Microsoft.

Tests:
- **Unit**: `@calendar_integration/tests/services/calendar_clients/test_ms_app_only_token.py` — token request shape, cache hit and expiry, the secret never logged (assert on `caplog`).
- **Integration**: `@calendar_integration/tests/services/test_microsoft_connection_service.py`:
  - Tampered state rejected.
  - Replayed nonce rejected.
  - Wrong org rejected.
  - Missing role gives a remediation message.
  - Success sets `write_enabled`.

  `@calendar_integration/tests/test_microsoft_connection_views.py` — admin only; flag off returns 404; the callback redirects only to `FRONTEND_BASE_URL`.

**Assigned to**: `tier3-2` (Tier 3) — an OAuth admin-consent round trip with signed state, plus a token cache. It starts the Microsoft track this member owns.

**Review models**: reviewer Tier 4 — an unauthenticated callback that binds a provider tenant to an organization; a state-validation bug links one customer's Microsoft tenant to another's organization.

**Reusable skills**: `add-env-var`, `create-rest-endpoint`, `write-unit-test`.

Acceptance: with the flag on, an org admin gets a consent URL; a valid callback stores the tenant id; verify sets `write_enabled=True` when the token carries both roles. A tampered or replayed state is rejected.

### Phase 6 — Microsoft Places write and location adapter

**Goal**: the Microsoft adapter, built from an organization connection, implements every `ResourceDirectoryAdapter` method using app-only tokens.

**Depends on**: Phase 2 (the `ResourceDirectoryAdapter` protocol, the room/location dataclasses and the `ResourceDirectoryError` family), Phase 5 (`MicrosoftAppOnlyTokenProvider` and `MicrosoftOrganizationConnection.tenant_id`).

**Feature flag**: `resource_calendar_provider_sync` — reachable only through the resolver (Phase 8), and only for write-enabled, flag-on orgs. Existing delegated-token adapter construction is untouched.

Changes:
1. [ms_outlook_calendar_api_client.py](../calendar_integration/services/calendar_clients/ms_outlook_calendar_api_client.py):
   - Add an app-only constructor that takes a token provider.
   - Add `create_room` (`POST /places`, `@odata.type=microsoft.graph.room`, `parentId`, `tags=["vinta-link-<uuid>"]`), `update_room` (`PATCH /places/{id}`), `delete_room` (`DELETE /places/{id}`), `find_room_by_tag`, `list_buildings`, `list_floors_and_sections`, and `get_schedule(room_emails, start, end)`.
   - Teach `_make_request` to honor 429 with `Retry-After` for a bounded number of attempts, and then raise a transient error.
2. [ms_outlook_calendar_adapter.py](../calendar_integration/services/calendar_adapters/ms_outlook_calendar_adapter.py): `from_app_only(connection)` builder, plus the protocol methods.
   - `create_room` calls `find_room_by_tag` before POST, so a replay returns the existing room.
   - `list_rooms` returns every room (no free/busy filter).
   - Descriptions follow the spike finding. Until it lands, they map to nothing on the provider side and stay vinta-only (marked `TODO(spike)` in one place).
   - Errors are classified as in Phase 4.

Spec use-case: shared scaffolding — no use-case yet (serves use-cases 1–4 on Microsoft).

Tests:
- **Unit**: [test_ms_outlook_calendar_api_client.py](../calendar_integration/tests/services/calendar_clients/test_ms_outlook_calendar_api_client.py) — request shapes per method; 429 with `Retry-After` retries, then raises a transient error; app-only auth header. [test_ms_outlook_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_ms_outlook_calendar_adapter.py) — the tag lookup prevents a duplicate create; mapping of building, floor and section to `ResourceLocationData`; error classification.

**Assigned to**: `tier3-2` (Tier 3) — Graph write calls, replay-safe create, and 429 handling. It continues the session from Phase 5.

**Reusable skills**: `write-unit-test`.

Acceptance: against mocked Graph responses, `create_room` called twice with the same link returns one room id and makes one POST.

### Phase 7 — Background push engine and sync-failure handling

**Goal**: any Vinta Schedule-side room change is pushed to the provider in the background with retries. After 6 hours a failing push ends in **sync failed**, with an admin email and a Sentry event, and it can be retried manually (spec use-case 5).

**Depends on**: Phase 2 (the `ResourceCalendarProviderLink` model, its helpers and queryset locks, `ResourceSyncStatus` / `ResourceSyncOperation`, the `ResourceDirectoryAdapterResolver` protocol, the `ResourceDirectoryError` family, and the `resource_room_synced` / `resource_room_archived` signals), Phase 3 (`RoomSyncNotifier.notify_sync_failed`).

**Feature flag**: `resource_calendar_provider_sync` — the engine has no caller until Phases 10–12c, which are gated. The task also re-checks the flag and exits as a no-op when it is off (a mid-flight flip leaves links untouched).

Changes:
1. `@calendar_integration/services/room_sync_service.py`: `RoomSyncService` (stateless, DI):
   - `request_push(link, operation)`: sets status (`PENDING_CREATION`, `PENDING_UPDATE` or `PENDING_DELETION`), sets `retry_deadline = now + 6h` when it isn't already set, and enqueues on commit.
   - `push(link_id)`: locks the link and dispatches on status to the resolver's adapter.
     - On success: `mark_pushed`, then `SYNCED` (or `ARCHIVED` after a delete), stores the provider's external id and email on the `Calendar` at create time, sends `resource_room_synced` / `resource_room_archived`, and writes an audit entry.
     - On a transient or permission error before the deadline: `attempt_count += 1` and reschedule.
     - On invalid input, or once the deadline has passed: `SYNC_FAILED` with `failed_operation` and `last_error`, `notify_sync_failed`, and `sentry_sdk.capture_message` with opaque ids.
   - `retry(link)`: only from `SYNC_FAILED`. It resets the attempts and deadline, then calls `request_push(failed_operation)`.
   - A link whose create never reached the provider and is then deleted goes straight to `ARCHIVED` without a provider call.
2. `@calendar_integration/tasks/room_sync_tasks.py`: `push_room_to_provider_task(link_id, organization_id)`, a `@app.task @inject` task with `Provide[...]` defaults (as `test_task_signatures.py` expects). It binds `organization_context` and computes the backoff countdown (1, 2, 4 … capped at 30 minutes). Export it in [tasks/__init__.py](../calendar_integration/tasks/__init__.py).
3. [di_core/containers.py](../di_core/containers.py): register `room_sync_service`. Its resolver dependency points at a provider named `resource_directory_adapter_resolver`, which Phase 8 fills. Until then, tests inject a fake.

Spec use-case: use-case 5 (sync keeps failing) and the push half of use-cases 1–3.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_room_sync_service.py`, using a fake resolver and adapter:
  - Each status transition in the spec's state diagram.
  - A replayed push after a successful create is idempotent.
  - A pending field changed again while the push was in flight is not cleared.
  - Invalid input fails immediately.
  - A transient error before the deadline reschedules; after the deadline it fails, notifies and captures to Sentry.
  - Retry from `SYNC_FAILED` re-pushes the failed operation.
  - The flag turned off mid-flight makes the push a no-op.
- **Integration**: `@calendar_integration/tests/tasks/test_room_sync_tasks.py` — the task binds the organization context; the countdown sequence; two concurrent pushes on one link serialize on the row lock. Plus [test_task_signatures.py](../calendar_integration/tests/tasks/test_task_signatures.py), extended for the new task.

**Assigned to**: `tier4` (Tier 4) — an acks-late, deadline-driven retry protocol with row locks and partial-field clearing. No precedent in this repo, and a subtle ordering bug loses edits or duplicates provider rooms.

**Reusable skills**: `write-unit-test`.

Acceptance: with a fake adapter that fails transiently for 6 hours and 1 minute, a link goes `PENDING_UPDATE` → `SYNC_FAILED`, exactly one admin email is queued, and `retry()` returns it to `PENDING_UPDATE`.

### Phase 8 — Wire provider adapters into the room sync resolver

**Goal**: the push engine, resync and busy check reach the real Google and Microsoft adapters. Google rooms that Vinta Schedule creates start event sync once they are synced.

**Depends on**: Phase 4 (Google `from_service_account(write=True)` and its `ResourceDirectoryAdapter` methods), Phase 6 (Microsoft `from_app_only(connection)` and its `ResourceDirectoryAdapter` methods), Phase 7 (the `resource_directory_adapter_resolver` DI provider name `RoomSyncService` consumes, and the `resource_room_synced(created=True)` send this receiver reacts to).

**Feature flag**: `resource_calendar_provider_sync` — `is_write_enabled` returns False when the flag is off; the signal receiver exits for flag-off orgs.

Changes:
1. `@calendar_integration/services/room_sync_adapter_resolver.py`: `RoomSyncAdapterResolver` implementing `ResourceDirectoryAdapterResolver`. Google uses the org-level service account (`calendar_fk__isnull=True`) with `write=True`. Microsoft uses `MicrosoftOrganizationConnection`. `is_write_enabled` checks the flag plus the matching `write_enabled`.
2. [di_core/containers.py](../di_core/containers.py): bind `resource_directory_adapter_resolver` to it.
3. `@calendar_integration/receivers/room_sync_receivers.py`, wired in [apps.py](../calendar_integration/apps.py): on `resource_room_synced(created=True, provider=GOOGLE)`, call `request_calendar_sync` for the room through the org service account, as the import does. This relies on the Google calendar-id fix already on main (see the prerequisites above **Goals**).

Spec use-case: shared scaffolding — wires use-cases 1–4 to real providers.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_room_sync_adapter_resolver.py`:
  - Picks the right adapter per provider.
  - Not write-enabled raises.
  - Flag off makes `is_write_enabled` False.
  - The receiver requests event sync for created Google rooms only.

**Assigned to**: `tier2` (Tier 2) — DI wiring and a signal receiver over finished adapters.

**Reusable skills**: `write-unit-test`.

Acceptance: `container.resource_directory_adapter_resolver()` returns an adapter for a write-enabled, flag-on org on either provider and raises for any other org.

### Phase 9 — Hourly location and room resync

**Goal**: every hour, each flag-on, write-enabled organization's locations and rooms match the provider. New provider rooms are imported, provider-changed fields win, and rooms deleted on the provider side are archived with their future bookings flagged (spec use-case 4).

**Depends on**: Phase 1 (`organization_ids_with_flag`), Phase 2 (`ResourceLocation`, `ResourceCalendarProviderLink.fields_changed_by_provider` / `locked_for_update(skip_locked=True)`, the `ResourceDirectoryAdapterResolver` protocol, and the `resource_room_synced` / `resource_room_archived` signals), Phase 3 (`RoomSyncNotifier.notify_edit_discarded` / `notify_bookings_flagged`).

**Feature flag**: `resource_calendar_provider_sync` — the beat task iterates `organization_ids_with_flag(...)` only. Flag-off orgs are never touched, and the on-demand import is unchanged.

Changes:
1. `@calendar_integration/services/room_resync_service.py`: `RoomResyncService.resync(organization, provider)`:
   - **Locations**: upsert from `list_locations`; deactivate the ones not seen. A location still referenced by a link is kept and marked inactive.
   - **Rooms**: from `list_rooms`:
     - (a) Link existing RESOURCE calendars with a matching `(external_id, provider)` that have no link yet (status `SYNCED`, snapshot = provider values).
     - (b) Import unknown rooms as new calendars plus `SYNCED` links, capped by the existing `resource_calendars` headroom helper (reused read-only from the sync service).
     - (c) For linked rooms, apply `fields_changed_by_provider`. Those fields overwrite `Calendar` and the snapshot. Pending edits to those fields are dropped (`notify_edit_discarded`, plus an audit entry with action `EXTERNAL_CHANGE_*`).
     - (d) A linked room missing from the provider (status `SYNCED` or `PENDING_UPDATE`): visibility `INACTIVE`, `archived_at`, status `ARCHIVED`. If it has future bookings, set `flagged_bookings_at` and send `notify_bookings_flagged`.
     - Links that are `PENDING_CREATION`, `PENDING_DELETION`, or locked by a push are skipped.
     - Send `resource_room_synced(created=False)` for each room linked in (a) or imported in (b), and `resource_room_archived` for each room archived in (d), on commit. This is how imported Microsoft rooms get event subscriptions in Phase 14b.
2. `@calendar_integration/tasks/room_resync_tasks.py`: a fan-out task `resync_rooms_for_flagged_organizations_task`, plus a per-org, per-provider `resync_organization_rooms_task` (bound to the org context; idempotent). Exported in [tasks/__init__.py](../calendar_integration/tasks/__init__.py).
3. [celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py): hourly entry for the fan-out task.
4. [di_core/containers.py](../di_core/containers.py): register `room_resync_service`.

Spec use-case: use-case 4 (room changed directly in the provider).

Tests:
- **Unit**: `@calendar_integration/tests/services/test_room_resync_service.py`, with a fake adapter:
  - Each branch (a)–(d).
  - Provider-changed field overrides a pending edit and notifies; a pending edit to an unchanged field survives.
  - Partial import at the plan limit.
  - Skipped statuses.
  - Location deactivation keeps referenced rows.
  - Spec acceptance scenario 6 end to end (rename, then delete with one booking → flagged).
- **Integration**: `@calendar_integration/tests/tasks/test_room_resync_tasks.py`:
  - The fan-out covers only flag-on orgs (a flag-off org's rooms are byte-for-byte unchanged).
  - A link locked by a concurrent push is skipped.
  - A re-run with no provider change writes nothing.

**Assigned to**: `tier4` (Tier 4) — per-field provider-wins against a snapshot, racing the push engine's row lock, plus limit-capped imports. It runs right after Phase 7 in the same session, so the push semantics are fresh.

**Review models**: reviewer Tier 4 — the overwrite rule decides whether a user's edit survives; a wrong comparison silently discards partner edits on every resync.

**Reusable skills**: `write-unit-test`.

Acceptance: for a flag-on org with a fake provider where room X was renamed and room Y deleted (with one future booking), one resync renames X, archives Y with `flagged_bookings_at` set, and queues one admin email per event type. A second resync writes nothing.

### Phase 10 — Create synced room and list locations

**Goal**: partners (GraphQL) and org admins (REST) can list provider locations and create a room on Google or Microsoft. It returns immediately as pending creation and becomes synced in the background (spec use-case 1).

**Depends on**: Phase 1 (`is_enabled`, which gates the `provider` input and the locations query), Phase 2 (`ResourceLocation`, `ResourceCalendarProviderLink`, `ResourceCalendarCreateRequest`), Phase 7 (`RoomSyncService.request_push(link, CREATE)`).

**Feature flag**: `resource_calendar_provider_sync`. **Off:** `provider` other than `INTERNAL` is rejected with the not-enabled message, the locations query and endpoint return the not-enabled error / 404, and omitting `provider` runs today's `create_resource_calendar` unchanged. **On:** the synced create path.

Changes:
1. [calendar_service.py](../calendar_integration/services/calendar_service.py): `create_synced_resource_calendar(provider, location_id, name, description, capacity, idempotency_key=None, ...)` in one transaction:
   - Flag check and `resolver.is_write_enabled`.
   - Active location of the same provider.
   - Idempotency lookup by `(org, key)`: same fingerprint returns the existing room; different fingerprint raises an error.
   - `check_limit(RESOURCE_CALENDARS, lock=True)`.
   - Create `Calendar` (`provider` set, `external_id=""`, `ACTIVE`), ownership and permissions as in `create_resource_calendar`.
   - Create the link (`PENDING_CREATION`, `pending_fields` = all synced fields), then `request_push`, then audit.
   - `create_resource_calendar` stays as is for `INTERNAL`.
2. A daily beat task `purge_expired_resource_calendar_create_requests_task` in `@calendar_integration/tasks/room_create_request_tasks.py`, plus an entry in [celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py).
3. [calendar_integration/graphql.py](../calendar_integration/graphql.py): `ResourceLocationGraphQLType`, `ResourceCalendarProviderSyncGraphQLType`, and `CalendarGraphQLType.provider_sync`.
4. [public_api/queries.py](../public_api/queries.py): paginated `resourceLocations`. [public_api/mutations.py](../public_api/mutations.py): `CreateResourceCalendarInput` gets `provider` / `location_id` / `idempotency_key`, and the resolver branches on `provider`. [public_api/constants.py](../public_api/constants.py) and [public_api/permissions.py](../public_api/permissions.py): `LIST_RESOURCE_LOCATIONS`.
5. [serializers.py](../calendar_integration/serializers.py) / [views.py](../calendar_integration/views.py): `ResourceCalendarCreateSerializer` gets the same three fields; new `GET /calendar/resource-locations/` (`IsOrganizationAdmin`).

Spec use-case: use-case 1 (partner creates a synced room).

Tests:
- **Integration**: `@calendar_integration/tests/services/test_create_synced_room.py`:
  - Spec acceptance scenarios 1–3 with a fake adapter (pending, then synced via an eager push; idempotent replay; not write-enabled is rejected with nothing created and limit usage unchanged).
  - Over the limit.
  - Inactive or other-provider location.
  - Key reused with a different payload.
  - **Flag off:** `provider` omitted gives the same row and audit as `create_resource_calendar` today; `provider=GOOGLE` is rejected.

  `@public_api/tests/test_synced_room_create_mutations.py` — grant enforcement, the `providerSync` field, pagination. `@calendar_integration/tests/test_synced_room_create_rest.py` — admin only; flag-off 404 on locations.
- **Unit**: `@calendar_integration/tests/tasks/test_room_create_request_tasks.py` — the purge deletes only expired rows.

**Assigned to**: `tier3-1` (Tier 3) — a transactional create that combines idempotency, limit locking and DI-dispatched push across GraphQL and REST.

**Reusable skills**: `create-graphql-public-query`, `create-rest-endpoint`, `write-unit-test`.

Acceptance: with the flag on and a write-enabled Google org, `createResourceCalendar(provider: GOOGLE, locationId, idempotencyKey: "K1")` returns `providerSync.status = PENDING_CREATION`. A repeat with K1 returns the same calendar id. With the flag off, the same mutation without `provider` behaves exactly as before this phase.

### Phase 11 — Edit synced room and retry sync

**Goal**: partners and org admins can edit a synced room's name, description, capacity and location, and retry a failed sync. Edits apply right away and push in the background (spec use-cases 2 and 5, step 4).

**Depends on**: Phase 7 (`RoomSyncService.request_push(link, UPDATE)` and `RoomSyncService.retry`), Phase 10 (the `CalendarGraphQLType.provider_sync` / `ResourceLocationGraphQLType` types the edit result returns, and the synced-room branches already added to `public_api/mutations.py`, `serializers.py` and `views.py`, which this phase extends).

**Feature flag**: `resource_calendar_provider_sync`. **Off:** `update_resource_calendar` keeps rejecting non-INTERNAL calendars with today's message, the generic REST update/destroy keep today's behavior, and `retryResourceCalendarSync` returns the not-enabled error. **On:** synced edits, the dedicated REST actions, and generic update/destroy rejecting synced rooms.

Changes:
1. [calendar_service.py](../calendar_integration/services/calendar_service.py) `update_resource_calendar`:
   - When the flag is on and the calendar has a link, accept `GOOGLE` / `MICROSOFT`.
   - Write the `Calendar` fields, merge the synced ones into `pending_fields`, and set `location` when given (active, same provider).
   - Call `request_push(UPDATE)` unless the link is `PENDING_CREATION`, in which case the edit merges into the pending create.
   - Reject when `ARCHIVED` or `PENDING_DELETION`.
   - `manage_available_windows`, `accepts_public_scheduling` and `visibility` stay vinta-only; `visibility=INACTIVE` on a synced room is rejected with "use deleteResourceCalendar".
   - Add `retry_resource_calendar_sync(calendar_id)`.
2. [public_api/mutations.py](../public_api/mutations.py): `UpdateResourceCalendarInput.location_id`, plus the `retryResourceCalendarSync` mutation. [public_api/constants.py](../public_api/constants.py) / [permissions.py](../public_api/permissions.py): `RETRY_RESOURCE_CALENDAR_SYNC`.
3. [serializers.py](../calendar_integration/serializers.py) / [views.py](../calendar_integration/views.py):
   - `PATCH /calendar/resource/{id}/` and `POST /calendar/resource/{id}/retry-sync/` (`IsOrganizationAdmin`).
   - With the flag on, `CalendarViewSet.update`, `partial_update` and `destroy` return 400 for calendars with a link, pointing at the dedicated actions.

Spec use-case: use-case 2 (partner edits a synced room), plus use-case 5 step 4 (retry).

Tests:
- **Integration**: `@calendar_integration/tests/services/test_edit_synced_room.py`:
  - Edit sets `PENDING_UPDATE` and `pending_fields`.
  - Edit during `PENDING_CREATION` merges.
  - Archived is rejected.
  - Location change on a different provider is rejected.
  - Spec acceptance scenario 7 (permission revoked → sync failed → retry → synced).
  - **Flag off:** a Google room is still rejected with today's message.

  `@public_api/tests/test_synced_room_edit_mutations.py` — the `capacity` UNSET/null/int semantics are preserved; retry grant enforcement. `@calendar_integration/tests/test_synced_room_edit_rest.py` — generic PATCH/DELETE on a synced room is rejected with the flag on and unchanged with it off.

**Assigned to**: `tier3-1` (Tier 3) — lifting a guard on a shared method without changing flag-off behavior, plus closing the plain-save REST path. It continues the session from Phase 10.

**Reusable skills**: `create-graphql-public-query`, `create-rest-endpoint`, `write-unit-test`.

Acceptance: with the flag on, `updateResourceCalendar(calendarId: <google room>, capacity: 10)` returns capacity 10 with `providerSync.status = PENDING_UPDATE`. With the flag off, the same call returns today's "synced from an external provider" error.

### Phase 12a — Room bookability, busy check and deletion preview

**Goal**: rooms that aren't bookable reject new bookings; a room's future bookings can be previewed with a fingerprint; and a booking-resolution plan can be validated all-or-nothing against busy, capacity and provider rules (first half of spec use-case 3).

**Depends on**: Phase 2 (`ResourceCalendarProviderLink.is_bookable`, the `ResourceDirectoryAdapterResolver.get_free_busy` contract, and `BusyWindow`).

**Feature flag**: `resource_calendar_provider_sync` — the bookability guard acts only on calendars with a link, and links exist only in flag-on orgs. The preview and validator have no caller until Phase 12c.

Changes:
1. `@calendar_integration/services/booking_resolution_service.py`: `BookingResolutionService` with:
   - `preview(room)`: future bookings = events on the room's calendar plus events with an active `ResourceAllocation` to the room. Recurring masters that started before now become one entry "from now on". The fingerprint is sha256 over sorted `(event id, modified, recurrence rule)`.
   - `room_busy_windows(room, start, end)`: events on the room's calendar, plus allocation-linked events with recurrences expanded, plus `resolver.adapter_for(...).get_free_busy(room.email, ...)` for synced rooms.
   - `validate(room, fingerprint, default_resolution, overrides) -> BookingResolutionPlan | list[RejectedBooking]`. It rejects a stale fingerprint; a move target that is the same room, not bookable, on a different provider, too small (`capacity` vs attendee count), or busy at any affected occurrence; and an override for an event not in the preview.
2. [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py): in the resource-allocation validation of `create_event` / `update_event`, reject any room whose link exists and is not `is_bookable` ("room is not bookable: pending creation / archived …").
3. [di_core/containers.py](../di_core/containers.py): register `booking_resolution_service`.

Spec use-case: use-case 3 (delete with bookings) — the preview and validation half.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_booking_resolution_preview.py`:
  - Series started in the past is one entry.
  - Allocation-only bookings are included.
  - Fingerprint changes when a booking is added or edited.
  - Every rejection reason, including provider-busy from a fake adapter and busy-by-allocation on the target.
  - Spec acceptance scenario 5 (busy target rejects with M1 named).
- **Integration**: `@calendar_integration/tests/services/test_room_bookability_guard.py` — allocating a `PENDING_CREATION` or `ARCHIVED` room is rejected; a room with no link (manual or flag-off org) books exactly as before.

**Assigned to**: `tier3-1` (Tier 3) — merging three busy sources over recurrence expansion, plus a validator with many rejection paths.

**Reusable skills**: `write-unit-test`.

Acceptance: for a room with one future booking at 10:00 and a target room busy at 10:00 by an allocation only, `validate(...)` with default MOVE rejects that booking with reason "target room is busy".

### Phase 12b — Booking resolution apply engine

**Goal**: a validated resolution plan can be applied: moved bookings switch rooms (whole events, or a series from now on), cancelled bookings lose the room or the whole event, and organizers are emailed (second half of spec use-case 3).

**Depends on**: Phase 12a (`BookingResolutionPlan` from `BookingResolutionService.validate`, and the module this phase extends). It also relies on the allocation-delete fix already on main ([vintasoftware/vinta-schedule-api#372](https://github.com/vintasoftware/vinta-schedule-api/pull/372)), which is not a plan phase.

**Feature flag**: `resource_calendar_provider_sync` — no caller until Phases 12c and 13, which are gated. The `create_recurring_event_bulk_modification` extension defaults to today's behavior (copy the parent's resources) when the new argument is omitted.

Changes:
1. [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py): `create_recurring_event_bulk_modification` (and the `modify_recurring_event_from_date` facade) gets an optional `resource_allocations_override`. When it is given, the continuation uses it instead of copying ([line 2636](../calendar_integration/services/calendar_event_service.py#L2636)).
2. `booking_resolution_service.py`: `apply(plan) -> ApplyResult(applied, pending, failed_at)`. Bookings are applied in order, each in its own transaction:
   - MOVE a whole event: `update_event` with the room swapped in `resource_allocations`.
   - MOVE a series from now on: `modify_recurring_event_from_date(..., resource_allocations_override=...)`.
   - CANCEL with REMOVE_ROOM: `update_event` without the room (or the series override without it).
   - CANCEL with CANCEL_EVENT: `delete_event`, or `cancel_recurring_event_from_date` for a series from now on.
   - After each step: `notify_booking_room_changed`.
   - On the first failure, stop and return the remaining bookings as pending.
   - A booking that no longer references the room is skipped (that is what makes a re-run idempotent).

Spec use-case: use-case 3 (delete with bookings) — the apply half.

Tests:
- **Integration**: `@calendar_integration/tests/services/test_booking_resolution_apply.py`:
  - Spec acceptance scenario 4 with a fake provider: M1 moved, a series moved from now on with past occurrences keeping room A, M2 cancelled for all attendees, organizers notified.
  - Remove-room keeps other events' allocations on the same room (regression guard for the allocation-delete fix in #372).
  - A failure at booking 2 of 3 stops with booking 1 applied and the rest pending; a re-run applies the rest only.
  - The bulk-modification override is absent by default, so today's copy behavior is unchanged.

**Assigned to**: `tier4` (Tier 4) — a multi-step apply over provider-backed event edits, including truncating and continuing a recurring series, with partial-failure semantics. A mistake corrupts real bookings.

**Reusable skills**: `write-unit-test`.

Acceptance: applying a plan for room A (M1 → B, series S → B from now on, M2 → cancel event) leaves no future occurrence referencing A, past occurrences of S still reference A, and M2 is cancelled.

### Phase 12c — Delete synced room

**Goal**: partners and org admins can preview and delete a synced room. Bookings are resolved per the plan, the room is archived and freed from the limit, and it is deleted from the provider in the background (spec use-case 3 end to end).

**Depends on**: Phase 7 (`RoomSyncService.request_push(link, DELETE)` and the never-pushed-create → `ARCHIVED` path), Phase 12b (`BookingResolutionService.apply` and `ApplyResult`), Phase 11 (the synced-room action set on `CalendarViewSet` and the `public_api/mutations.py` / `permissions.py` / `constants.py` blocks this phase extends; serialized to avoid colliding edits).

**Feature flag**: `resource_calendar_provider_sync` — the preview query, the delete mutation and the REST actions return the not-enabled error / 404 when the flag is off. `disableResourceCalendar` and the generic REST destroy for manual rooms are unchanged.

Changes:
1. [calendar_service.py](../calendar_integration/services/calendar_service.py): `delete_synced_resource_calendar(calendar_id, fingerprint, default_resolution, overrides)`:
   - Rejects manual rooms ("use disableResourceCalendar") and archived rooms (no-op success, for idempotency).
   - `ABORT` with any booking returns the booking list and changes nothing.
   - Otherwise it runs validate and then apply. If the apply is incomplete, it returns the result with the room untouched.
   - If everything is resolved: visibility `INACTIVE`, `archived_at`, then `request_push(DELETE)` (or direct `ARCHIVED` if the room never reached the provider), then audit.
2. [public_api/queries.py](../public_api/queries.py): `resourceCalendarDeletionPreview`. [public_api/mutations.py](../public_api/mutations.py): `deleteResourceCalendar` with the resolution input types (reused by Phase 13). Constants and permissions: `PREVIEW_RESOURCE_CALENDAR_DELETION`, `DELETE_RESOURCE_CALENDAR`.
3. [serializers.py](../calendar_integration/serializers.py) / [views.py](../calendar_integration/views.py): `GET /calendar/resource/{id}/deletion-preview/` and `POST /calendar/resource/{id}/delete/`, with resolution serializers reused by Phase 13.

Spec use-case: use-case 3 (partner deletes a room with future bookings).

Tests:
- **Integration**: `@calendar_integration/tests/services/test_delete_synced_room.py`:
  - Spec acceptance scenarios 4 and 5 end to end with a fake adapter. Room A ends `ARCHIVED`, its limit usage drops by 1, and the provider delete was called once.
  - Stale fingerprint is rejected.
  - Pending-creation room deleted means no provider call.
  - Delete twice is a no-op success.
  - Partial apply leaves the room `SYNCED`.
  - **Flag off:** the mutation is rejected and `disableResourceCalendar` is unchanged.

  `@public_api/tests/test_synced_room_delete_mutations.py` — grants, input validation. `@calendar_integration/tests/test_synced_room_delete_rest.py` — admin only; flag-off 404.

**Assigned to**: `tier3-1` (Tier 3) — orchestrating the validator, the engine and the push engine, plus two surfaces. It continues the session from Phase 11.

**Reusable skills**: `create-graphql-public-query`, `create-rest-endpoint`, `write-unit-test`.

Acceptance: with the flag on, `deleteResourceCalendar(calendarId: A, fingerprint, defaultResolution: MOVE, targetCalendarId: B)` on a room with movable bookings returns success. A is `PENDING_DELETION`, then `ARCHIVED` after the push, and no future booking references A.

### Phase 13 — Resolve bookings flagged by provider-side deletion

**Goal**: for a room the provider deleted (archived and flagged by the resync), an admin or partner can resolve its future bookings with the same move/cancel options (spec use-case 4, step 4).

**Depends on**: Phase 9 (`flagged_bookings_at`, set by the resync on provider-deleted rooms), Phase 12b (`BookingResolutionService.validate` / `apply`), Phase 12c (the resolution GraphQL input types and REST serializers this phase reuses, and the delete surface it sits beside in `public_api/mutations.py` / `views.py`).

**Feature flag**: `resource_calendar_provider_sync` — the mutation and REST action return the not-enabled error / 404 when the flag is off.

Changes:
1. [calendar_service.py](../calendar_integration/services/calendar_service.py): `resolve_flagged_resource_bookings(calendar_id, fingerprint, default_resolution, overrides)`. Only for `ARCHIVED` links with `flagged_bookings_at` set. `ABORT` isn't accepted, because the room is already gone. It runs validate and apply, then clears `flagged_bookings_at` once there are no future bookings left.
2. [public_api/mutations.py](../public_api/mutations.py): `resolveFlaggedResourceBookings`. Constants and permissions: `RESOLVE_FLAGGED_RESOURCE_BOOKINGS`. The deletion preview query also accepts archived, flagged rooms.
3. [views.py](../calendar_integration/views.py): `POST /calendar/resource/{id}/resolve-flagged-bookings/`.

Spec use-case: use-case 4 (room deleted in provider → flagged bookings resolved).

Tests:
- **Integration**: `@calendar_integration/tests/services/test_resolve_flagged_bookings.py`:
  - Resolve with MOVE clears the flag.
  - A partial apply keeps the flag.
  - Non-flagged room is rejected.
  - ABORT is rejected.

  `@public_api/tests/test_flagged_bookings_mutations.py` — grant; flag off is rejected.

**Assigned to**: `tier2` (Tier 2) — a thin service plus surfaces over an engine and input types that already exist.

**Reusable skills**: `create-graphql-public-query`, `write-unit-test`.

Acceptance: for an archived, flagged room with one future booking, `resolveFlaggedResourceBookings(defaultResolution: MOVE, targetCalendarId: B)` moves the booking and clears `providerSync.flaggedBookingsAt`.

### Phase 14a — Microsoft app-only room event delta sync

**Goal**: bookings on every synced Microsoft room in a flag-on, write-enabled org sync into Vinta Schedule with app-only credentials.

**Depends on**: Phase 1 (`is_enabled`, to limit sync to flag-on orgs), Phase 6 (the app-only `MSOutlookCalendarAPIClient` constructor, `MSOutlookCalendarAdapter.from_app_only`, and 429 handling).

**Feature flag**: `resource_calendar_provider_sync` — only rooms in flag-on, write-enabled orgs are synced through this path. Delegated-token event sync is unchanged.

Changes:
1. [ms_outlook_calendar_api_client.py](../calendar_integration/services/calendar_clients/ms_outlook_calendar_api_client.py): `get_room_calendar_view_delta(room_email, start, end, delta_link)` (`/users/{email}/calendarView/delta`, app-only).
2. [ms_outlook_calendar_adapter.py](../calendar_integration/services/calendar_adapters/ms_outlook_calendar_adapter.py): `get_room_events(...)` returns the same shape `get_events` returns, so the existing sync pipeline consumes it.
3. [calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py): an entry point `sync_microsoft_room_events(calendar)`. It authenticates with the app-only adapter and reuses `_execute_calendar_sync` through a `CalendarSync` row with the delta link stored as `next_sync_token`.
4. `@calendar_integration/tasks/room_event_sync_tasks.py`: `sync_microsoft_room_events_task(calendar_id, organization_id)` (idempotent; bound to the org context). Exported in [tasks/__init__.py](../calendar_integration/tasks/__init__.py).

Spec use-case: shared scaffolding — no spec use-case (Step-0 decision: Microsoft room event sync is in scope).

Tests:
- **Unit**: [test_ms_outlook_calendar_api_client.py](../calendar_integration/tests/services/calendar_clients/test_ms_outlook_calendar_api_client.py) (delta paging, the delta link persisted). `@calendar_integration/tests/services/test_ms_room_event_sync.py` — first sync creates events; second sync with the delta link updates and deletes; flag-off org is skipped.
- **Integration**: `@calendar_integration/tests/tasks/test_room_event_sync_tasks.py` — task scoping; a re-run is idempotent.

**Assigned to**: `tier3-2` (Tier 3) — plugging an app-only delta source into the existing sync pipeline without disturbing delegated sync.

**Reusable skills**: `write-unit-test`.

Acceptance: for a flag-on, write-enabled org, running `sync_microsoft_room_events_task` on a Microsoft room with two provider events creates two `CalendarEvent`s; a second run after one deletion removes one.

### Phase 14b — Microsoft room event webhooks and renewal

**Goal**: Microsoft room bookings reach Vinta Schedule within minutes, through Graph change notifications that trigger the delta sync. Subscriptions follow the room lifecycle and are renewed before they expire.

**Depends on**: Phase 2 (the `resource_room_synced` / `resource_room_archived` signals that drive subscribe and unsubscribe; Phases 7 and 9 send them, and the tests send them directly), Phase 14a (`sync_microsoft_room_events_task`, which a notification enqueues).

**Feature flag**: `resource_calendar_provider_sync` — subscriptions are created only for flag-on orgs; the renewal beat iterates flag-on orgs only; notifications for unknown or inactive subscriptions are ignored, as today.

Changes:
1. [ms_outlook_calendar_api_client.py](../calendar_integration/services/calendar_clients/ms_outlook_calendar_api_client.py): app-only `create_subscription` / `renew_subscription` / `delete_subscription` for `/users/{email}/events`, with `clientState` set to a random secret stored in `CalendarWebhookSubscription.verification_token`.
2. [calendar_webhook_service.py](../calendar_integration/services/calendar_webhook_service.py): `subscribe_microsoft_room(calendar)`, `unsubscribe_microsoft_room(calendar)` and `renew_expiring_microsoft_room_subscriptions()`, stored in `CalendarWebhookSubscription`.
3. [webhook_views.py](../calendar_integration/webhook_views.py): the Microsoft room notification path. It answers the `validationToken` handshake, checks `clientState` in constant time, looks up the subscription with an explicit `filter_by_organization` (a plain `View`), and enqueues `sync_microsoft_room_events_task` on commit.
4. `@calendar_integration/receivers/ms_room_subscription_receivers.py` (wired in [apps.py](../calendar_integration/apps.py)): subscribe on `resource_room_synced` for Microsoft rooms (both `created=True` from the push engine and `created=False` from the resync), and unsubscribe on `resource_room_archived`.
5. [celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py) plus `room_event_sync_tasks.py`: a renewal task every 12 hours (Graph event subscriptions expire in under 3 days) and a daily fallback delta sweep.

Spec use-case: shared scaffolding — no spec use-case (Step-0 decision: webhooks plus delta).

Tests:
- **Integration**: `@calendar_integration/tests/services/test_ms_room_webhooks.py`:
  - Subscribe on synced, unsubscribe on archived.
  - Renewal picks only subscriptions expiring within 24h in flag-on orgs.

  `@calendar_integration/tests/test_ms_room_webhook_views.py`:
  - Validation handshake echoes the token.
  - Wrong `clientState` gets 403 with no task enqueued.
  - Valid notification enqueues one delta sync.

**Assigned to**: `tier3-2` (Tier 3) — an unauthenticated inbound webhook with a secret check, plus subscription lifecycle tied to signals. It continues the session from Phase 14a.

**Reusable skills**: `write-unit-test`.

Acceptance: a valid Graph notification for a subscribed room enqueues exactly one `sync_microsoft_room_events_task`; a notification with a wrong `clientState` enqueues nothing.

### Phase 15 — Django admin for room sync operations

**Goal**: Vinta ops can see every room link's sync status, last error and flagged bookings, retry failed syncs, and inspect or verify Microsoft connections and locations from Django admin.

**Depends on**: Phase 2 (the models being registered), Phase 5 (`MicrosoftConnectionService.verify` for the verify admin action), Phase 7 (`RoomSyncService.retry` for the retry admin action).

**Feature flag**: none — staff-only ops surface; actions call services that enforce the flag themselves.

Changes:
1. [calendar_integration/admin.py](../calendar_integration/admin.py):
   - `ResourceCalendarProviderLinkAdmin`: list by status, provider and organization; read-only snapshot and pending fields; a "Retry sync" action calling `RoomSyncService.retry` per selected `SYNC_FAILED` link.
   - `ResourceLocationAdmin` (read-only).
   - `MicrosoftOrganizationConnectionAdmin` (read-only `tenant_id`; a "Verify" action).
   - Querysets go through `original_manager` with a comment (admin is cross-org by design). Use `unscoped_default_manager()` only around FK form fields, per CLAUDE.md.

Spec use-case: use-case 5 (ops visibility and manual retry) — Django admin entry point.

Tests:
- **Integration**: `@calendar_integration/tests/test_room_sync_admin.py` — the changelists render for a superuser; the retry action moves a `SYNC_FAILED` link to pending and ignores links in other statuses; the verify action calls the service.

**Assigned to**: `tier2` (Tier 2) — admin registrations with two actions, following existing admin precedent.

**Reusable skills**: `write-unit-test`.

Acceptance: a superuser selecting a `SYNC_FAILED` link and running "Retry sync" moves it back to its pending status and enqueues one push.

### Phase 16 — Remove the `resource_calendar_provider_sync` feature flag

**Goal**: delete the flag and its dead off-branches, so provider room sync becomes unconditional. **Prerequisite**: the flag has been on for 100% of organizations in production for at least 2 weeks, with no rollback or incident attributed to it.

**Depends on**: every gated phase, since this phase deletes the branches they added: Phase 4 (the verify-endpoint gate), Phase 5 (the consent-endpoint gates), Phase 8 (the `is_write_enabled` / receiver flag checks), Phase 7 (the in-task flag re-check), Phase 9 (the flag-filtered fan-out), Phase 10 (the `provider`-input and locations gates), Phase 11 (the synced-edit and generic-REST gates), Phase 12a (the link-presence assumption in the bookability guard), Phase 12c (the delete-surface gates), Phase 13 (the flagged-resolution gate), Phase 14a (the flag-on org filter for room event sync), Phase 14b (the subscription and renewal gates).

**Feature flag**: removed in this phase.

Changes:
1. [common/feature_flags.py](../common/feature_flags.py): delete `RESOURCE_CALENDAR_PROVIDER_SYNC`. The module and `OrganizationFeatureFlag` stay as infrastructure for future flags. Add a data migration that deletes the `OrganizationFeatureFlag` rows with this key (reverse: no-op).
2. Inline the on-branch and delete the off-branch at every check site:
   - [google_write_access_views.py](../calendar_integration/google_write_access_views.py)
   - [microsoft_connection_views.py](../calendar_integration/microsoft_connection_views.py)
   - [room_sync_adapter_resolver.py](../calendar_integration/services/room_sync_adapter_resolver.py)
   - [room_sync_receivers.py](../calendar_integration/receivers/room_sync_receivers.py)
   - [room_sync_service.py](../calendar_integration/services/room_sync_service.py)
   - [room_resync_tasks.py](../calendar_integration/tasks/room_resync_tasks.py) (the fan-out iterates write-enabled orgs instead)
   - [calendar_service.py](../calendar_integration/services/calendar_service.py)
   - [views.py](../calendar_integration/views.py)
   - [public_api/mutations.py](../public_api/mutations.py)
   - [public_api/queries.py](../public_api/queries.py)
   - [room_event_sync_tasks.py](../calendar_integration/tasks/room_event_sync_tasks.py)
   - [calendar_webhook_service.py](../calendar_integration/services/calendar_webhook_service.py)
   - [ms_room_subscription_receivers.py](../calendar_integration/receivers/ms_room_subscription_receivers.py)
3. Delete the flag-off tests added in Phases 4, 5, 9, 10, 11, 12c, 13 and 14a/14b, and simplify fixtures that toggled the flag.
4. `grep -r "resource_calendar_provider_sync\|RESOURCE_CALENDAR_PROVIDER_SYNC"` returns only the data migration.

Tests:
- The existing suite passes on the former on-branch; flag-parametrized tests are removed.

**Assigned to**: `tier2` (Tier 2) — mechanical deletion and inlining. Every removed line was added by a phase above, so there is exact precedent.

**Reusable skills**: `add-migration` (for the flag-row cleanup data migration).

Acceptance: `grep -r "RESOURCE_CALENDAR_PROVIDER_SYNC" --include=*.py . | grep -v migrations` returns nothing, the feature behaves as it did with the flag on, and the full suite is green.

## 6. Risk & Rollout Notes

- **Feature flag rollout** (`resource_calendar_provider_sync`, per org, default off):
  1. Enable for an internal Vinta org on staging, connected to sandbox Google Workspace and Microsoft 365 tenants.
  2. Run the spec's **Objectives** end-to-end test with the partner on both providers.
  3. 48h soak on staging, with no `SYNC_FAILED` caused by our code.
  4. Enable for one pilot production org, then a cohort, then all.
  5. Phase 16 runs 2 weeks after 100%.
  - **Rollback:** turn the flag off. Pushes become no-ops, the resync stops, the endpoints disappear, and links and data stay put for re-enable.
- **Migrations:**
  - Phase 2 is additive: new tables, plus two defaulted columns on `GoogleCalendarServiceAccount` (a small table, so no lock concern).
  - Phase 1 adds one table.
  - Phase 16 adds a data migration with a no-op reverse.
  - `Calendar` (hot) is not altered.
  - All are reversible by migrating back.
- **Provider deletion is a one-way door.** Mitigations: the preview fingerprint, the flag-gated surface, archived rows kept in Vinta Schedule, and audit entries. There is no un-delete.
- **Partial apply in the delete flow.** A provider failure mid-apply leaves some bookings moved and the room not deleted. This is reported to the caller, and a re-preview plus delete finishes it. Accepted; see **Guiding Decisions → Delete apply semantics**.
- **Duplicate provider rooms on replay.** Google: covered by the deterministic `resourceId`. Microsoft: covered by the tag lookup. The spike (Phase 0) confirms that tags round-trip. If they don't, `amend-plan` switches Microsoft to a displayName-plus-parent lookup.
- **Scope split (Google).** Existing customers granted only the read-only scope. Building the write client only for `write_enabled` accounts avoids breaking their imports. A test in Phase 4 asserts it.
- **Microsoft setup is manual for customers** (admin consent plus the Exchange RBAC assignment). Documented in Phase 5. A missing RBAC role shows up as a sync failure, and admins get a remediation message.
- **Manual ops pre-steps before staging end-to-end testing:**
  1. Vinta ops registers the multi-tenant Entra app with its redirect URI and the `Place.ReadWrite.All` and `Calendars.Read` app permissions.
  2. Set `MS_CLIENT_ID` and `MS_CLIENT_SECRET` in the staging Secrets Manager secret.
  3. Get sandbox Google Workspace and Microsoft 365 tenants for the spike and the end-to-end test.
- **Graph throttling and Directory quotas.** The hourly resync is one listing per org per provider, plus locations. 429s are honored with `Retry-After` (Phase 6). Accepted risk for tenants with a few hundred rooms (spec **Risks assumed**).
- **Prerequisites outside this plan.** Both are on main: the allocation-delete fix (#372), which Phase 12b relies on, and the Google room calendar-id fix (#373, #375), which Phase 8's Google room event sync relies on. A revert of either would put those phases back at risk.
- **Security/compliance.** Organization-wide directory-write credentials:
  - Google keys are already encrypted.
  - Microsoft stores only `tenant_id`, and the Vinta app secret lives in Secrets Manager.
  - Every write is audited.
  - The two unauthenticated surfaces (consent callback and room webhook) get a Tier 4 review or a constant-time secret check.

  Raise with the project lead before the production flag flip.
- **PHI.** Room names and descriptions aren't PHI, but bookings can carry PHI in titles. Notification templates, Sentry messages, logs and the spike script use only opaque ids and room names, never event titles or attendee data.

## 7. Open Questions

1. **Does deleting a Graph room remove its mailbox?** (spec open question 1) — Recommended default: treat the room as deleted once the place is gone, and document any leftover mailbox. Owner: whoever runs the Phase 0 spike. Unblocks: confidence in the Microsoft delete path.
2. **Where does a Microsoft room's description live?** (spec open question 2) — Recommended default: description stays vinta-only for Microsoft rooms until the spike finds a writable field. Owner: Phase 0 spike, with product (Hugo) confirming. Unblocks: whether Phase 6 maps description.
3. **Google building required, and duplicate-name behavior?** (spec open question 8) — Recommended default: always require a location (already enforced); pass provider duplicate-name errors through as invalid input. Owner: Phase 0 spike.
4. **Does Microsoft `getSchedule` (app-only) need anything beyond `Calendars.Read` for room mailboxes?** — Recommended default: `Calendars.Read` is enough; if not, add the permission to the Entra app and the setup doc. Owner: Phase 0 spike / Phase 6 implementer.
5. **Who flips the production flag, and who approves?** — Recommended default: the project lead, after the staging soak and the security review in **Risk & Rollout Notes**. Owner: Hugo.

## 8. Touch List

**Phase 0**
- @scripts/spikes/resource_directory_spike.py
- @scripts/spikes/tests/test_resource_directory_spike.py
- @docs/integrations/resource-directory-spike.md

**Phase 1**
- [organizations/models.py](../organizations/models.py)
- @organizations/migrations/ (new migration)
- [organizations/admin.py](../organizations/admin.py)
- @common/feature_flags.py
- @common/tests/test_feature_flags.py
- @organizations/tests/test_feature_flag_admin.py

**Phase 2**
- [calendar_integration/constants.py](../calendar_integration/constants.py)
- [calendar_integration/models.py](../calendar_integration/models.py)
- [calendar_integration/querysets.py](../calendar_integration/querysets.py)
- [calendar_integration/managers.py](../calendar_integration/managers.py)
- @calendar_integration/migrations/ (new migration)
- [calendar_integration/services/dataclasses.py](../calendar_integration/services/dataclasses.py)
- @calendar_integration/services/protocols/resource_directory_adapter.py
- [calendar_integration/exceptions.py](../calendar_integration/exceptions.py)
- [calendar_integration/signals.py](../calendar_integration/signals.py)
- [calendar_integration/factories.py](../calendar_integration/factories.py)
- @calendar_integration/tests/models/test_room_provider_sync_models.py

**Phase 3**
- @calendar_integration/services/room_sync_notifier.py
- [calendar_integration/notification_contexts.py](../calendar_integration/notification_contexts.py)
- @templates/calendar_integration/emails/ (four templates × three files)
- [di_core/containers.py](../di_core/containers.py)
- @calendar_integration/tests/services/test_room_sync_notifier.py

**Phase 4**
- [google_calendar_adapter.py](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py)
- @calendar_integration/services/google_write_access_service.py
- @calendar_integration/google_write_access_views.py
- [calendar_integration/routes.py](../calendar_integration/routes.py)
- [di_core/containers.py](../di_core/containers.py)
- [test_google_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_google_calendar_adapter.py)
- @calendar_integration/tests/services/test_google_write_access_service.py
- @calendar_integration/tests/test_google_write_access_views.py

**Phase 5**
- [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py)
- .env.example
- .env.docker.example
- [infrastructure/modules/app-platform/ecs.tf](../infrastructure/modules/app-platform/ecs.tf)
- [infrastructure/modules/app-platform/secrets.tf](../infrastructure/modules/app-platform/secrets.tf)
- [CLAUDE.md](../CLAUDE.md)
- @calendar_integration/services/calendar_clients/ms_app_only_token.py
- @calendar_integration/services/microsoft_connection_service.py
- @calendar_integration/microsoft_connection_views.py
- [calendar_integration/routes.py](../calendar_integration/routes.py)
- [di_core/containers.py](../di_core/containers.py)
- @docs/integrations/microsoft-room-sync-setup.md
- @calendar_integration/tests/services/calendar_clients/test_ms_app_only_token.py
- @calendar_integration/tests/services/test_microsoft_connection_service.py
- @calendar_integration/tests/test_microsoft_connection_views.py

**Phase 6**
- [ms_outlook_calendar_api_client.py](../calendar_integration/services/calendar_clients/ms_outlook_calendar_api_client.py)
- [ms_outlook_calendar_adapter.py](../calendar_integration/services/calendar_adapters/ms_outlook_calendar_adapter.py)
- [test_ms_outlook_calendar_api_client.py](../calendar_integration/tests/services/calendar_clients/test_ms_outlook_calendar_api_client.py)
- [test_ms_outlook_calendar_adapter.py](../calendar_integration/tests/services/calendar_adapters/test_ms_outlook_calendar_adapter.py)

**Phase 7**
- @calendar_integration/services/room_sync_service.py
- @calendar_integration/tasks/room_sync_tasks.py
- [calendar_integration/tasks/__init__.py](../calendar_integration/tasks/__init__.py)
- [di_core/containers.py](../di_core/containers.py)
- @calendar_integration/tests/services/test_room_sync_service.py
- @calendar_integration/tests/tasks/test_room_sync_tasks.py
- [test_task_signatures.py](../calendar_integration/tests/tasks/test_task_signatures.py)

**Phase 8**
- @calendar_integration/services/room_sync_adapter_resolver.py
- @calendar_integration/receivers/__init__.py
- @calendar_integration/receivers/room_sync_receivers.py
- [calendar_integration/apps.py](../calendar_integration/apps.py)
- [di_core/containers.py](../di_core/containers.py)
- @calendar_integration/tests/services/test_room_sync_adapter_resolver.py

**Phase 9**
- @calendar_integration/services/room_resync_service.py
- @calendar_integration/tasks/room_resync_tasks.py
- [calendar_integration/tasks/__init__.py](../calendar_integration/tasks/__init__.py)
- [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py)
- [di_core/containers.py](../di_core/containers.py)
- @calendar_integration/tests/services/test_room_resync_service.py
- @calendar_integration/tests/tasks/test_room_resync_tasks.py

**Phase 10**
- [calendar_service.py](../calendar_integration/services/calendar_service.py)
- @calendar_integration/tasks/room_create_request_tasks.py
- [calendar_integration/tasks/__init__.py](../calendar_integration/tasks/__init__.py)
- [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py)
- [calendar_integration/graphql.py](../calendar_integration/graphql.py)
- [public_api/queries.py](../public_api/queries.py)
- [public_api/mutations.py](../public_api/mutations.py)
- [public_api/constants.py](../public_api/constants.py)
- [public_api/permissions.py](../public_api/permissions.py)
- [calendar_integration/serializers.py](../calendar_integration/serializers.py)
- [calendar_integration/views.py](../calendar_integration/views.py)
- @calendar_integration/tests/services/test_create_synced_room.py
- @calendar_integration/tests/tasks/test_room_create_request_tasks.py
- @public_api/tests/test_synced_room_create_mutations.py
- @calendar_integration/tests/test_synced_room_create_rest.py

**Phase 11**
- [calendar_service.py](../calendar_integration/services/calendar_service.py)
- [public_api/mutations.py](../public_api/mutations.py)
- [public_api/constants.py](../public_api/constants.py)
- [public_api/permissions.py](../public_api/permissions.py)
- [calendar_integration/serializers.py](../calendar_integration/serializers.py)
- [calendar_integration/views.py](../calendar_integration/views.py)
- @calendar_integration/tests/services/test_edit_synced_room.py
- @public_api/tests/test_synced_room_edit_mutations.py
- @calendar_integration/tests/test_synced_room_edit_rest.py

**Phase 12a**
- @calendar_integration/services/booking_resolution_service.py
- [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py)
- [di_core/containers.py](../di_core/containers.py)
- @calendar_integration/tests/services/test_booking_resolution_preview.py
- @calendar_integration/tests/services/test_room_bookability_guard.py

**Phase 12b**
- [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py)
- [calendar_service.py](../calendar_integration/services/calendar_service.py) (`modify_recurring_event_from_date` facade signature only)
- @calendar_integration/services/booking_resolution_service.py
- @calendar_integration/tests/services/test_booking_resolution_apply.py

**Phase 12c**
- [calendar_service.py](../calendar_integration/services/calendar_service.py)
- [public_api/queries.py](../public_api/queries.py)
- [public_api/mutations.py](../public_api/mutations.py)
- [public_api/constants.py](../public_api/constants.py)
- [public_api/permissions.py](../public_api/permissions.py)
- [calendar_integration/serializers.py](../calendar_integration/serializers.py)
- [calendar_integration/views.py](../calendar_integration/views.py)
- @calendar_integration/tests/services/test_delete_synced_room.py
- @public_api/tests/test_synced_room_delete_mutations.py
- @calendar_integration/tests/test_synced_room_delete_rest.py

**Phase 13**
- [calendar_service.py](../calendar_integration/services/calendar_service.py)
- [public_api/queries.py](../public_api/queries.py)
- [public_api/mutations.py](../public_api/mutations.py)
- [public_api/constants.py](../public_api/constants.py)
- [public_api/permissions.py](../public_api/permissions.py)
- [calendar_integration/views.py](../calendar_integration/views.py)
- @calendar_integration/tests/services/test_resolve_flagged_bookings.py
- @public_api/tests/test_flagged_bookings_mutations.py

**Phase 14a**
- [ms_outlook_calendar_api_client.py](../calendar_integration/services/calendar_clients/ms_outlook_calendar_api_client.py)
- [ms_outlook_calendar_adapter.py](../calendar_integration/services/calendar_adapters/ms_outlook_calendar_adapter.py)
- [calendar_sync_service.py](../calendar_integration/services/calendar_sync_service.py)
- @calendar_integration/tasks/room_event_sync_tasks.py
- [calendar_integration/tasks/__init__.py](../calendar_integration/tasks/__init__.py)
- [test_ms_outlook_calendar_api_client.py](../calendar_integration/tests/services/calendar_clients/test_ms_outlook_calendar_api_client.py)
- @calendar_integration/tests/services/test_ms_room_event_sync.py
- @calendar_integration/tests/tasks/test_room_event_sync_tasks.py

**Phase 14b**
- [ms_outlook_calendar_api_client.py](../calendar_integration/services/calendar_clients/ms_outlook_calendar_api_client.py)
- [calendar_webhook_service.py](../calendar_integration/services/calendar_webhook_service.py)
- [webhook_views.py](../calendar_integration/webhook_views.py)
- @calendar_integration/receivers/ms_room_subscription_receivers.py
- [calendar_integration/apps.py](../calendar_integration/apps.py)
- [vinta_schedule_api/celerybeat_schedule.py](../vinta_schedule_api/celerybeat_schedule.py)
- @calendar_integration/tasks/room_event_sync_tasks.py
- @calendar_integration/tests/services/test_ms_room_webhooks.py
- @calendar_integration/tests/test_ms_room_webhook_views.py

**Phase 15**
- [calendar_integration/admin.py](../calendar_integration/admin.py)
- @calendar_integration/tests/test_room_sync_admin.py

**Phase 16**
- [common/feature_flags.py](../common/feature_flags.py)
- @organizations/migrations/ (data migration removing flag rows)
- [calendar_integration/google_write_access_views.py](../calendar_integration/google_write_access_views.py)
- [calendar_integration/microsoft_connection_views.py](../calendar_integration/microsoft_connection_views.py)
- [room_sync_adapter_resolver.py](../calendar_integration/services/room_sync_adapter_resolver.py)
- [room_sync_receivers.py](../calendar_integration/receivers/room_sync_receivers.py)
- [room_sync_service.py](../calendar_integration/services/room_sync_service.py)
- [room_resync_tasks.py](../calendar_integration/tasks/room_resync_tasks.py)
- [calendar_service.py](../calendar_integration/services/calendar_service.py)
- [calendar_integration/views.py](../calendar_integration/views.py)
- [public_api/mutations.py](../public_api/mutations.py)
- [public_api/queries.py](../public_api/queries.py)
- [room_event_sync_tasks.py](../calendar_integration/tasks/room_event_sync_tasks.py)
- [calendar_webhook_service.py](../calendar_integration/services/calendar_webhook_service.py)
- [ms_room_subscription_receivers.py](../calendar_integration/receivers/ms_room_subscription_receivers.py)
- the flag-off tests listed in Phase 16 → Changes, step 3
