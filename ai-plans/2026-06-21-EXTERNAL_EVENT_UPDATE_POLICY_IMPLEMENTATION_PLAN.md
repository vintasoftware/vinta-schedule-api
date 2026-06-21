# External Event Update Policy — Implementation Plan

> Per-organization control over how inbound external-provider (Google Calendar) edits and
> deletions to existing synced events are handled: applied directly, routed through an
> approval workflow, or forbidden (auto-undone). No `..._SPEC.md` sibling exists; this plan
> stands alone and encodes the decisions captured in planning.

## 1. Goals

1. Add a per-organization policy, `external_event_update_policy`, with three modes — `ALLOW`
   (apply inbound external edits directly, today's behavior), `CHANGE_REQUEST` (route inbound
   edits/deletions into an approval record), and `FORBIDDEN` (auto-undo inbound edits/deletions
   on the external provider).
2. Under `CHANGE_REQUEST`, intercept inbound updates (title / description / start_time /
   end_time) **and** deletions to existing synced events, persist them as
   `ExternalEventChangeRequest` records instead of mutating the local event, and notify the
   eligible approvers in-app.
3. Let an event's **member-attendees** approve/reject change requests on their own events, and
   **organization admins** approve/reject any event's requests. Approval applies the change
   locally; rejection (and `FORBIDDEN` auto-undo) pushes the retained value back to Google
   Calendar — re-creating the event when the inbound change was a deletion.
4. Expose listing + approve/reject through both the internal REST API and the public GraphQL
   API, and record every create/approve/reject/auto-undo in the audit trail.
5. Make `CHANGE_REQUEST` the default policy for all organizations from day one (the app is
   pre-launch — no existing tenants to migrate or grandfather).

**Non-goals (v1):**
- Attendee-list / RSVP-only inbound changes — those continue to apply directly (the
  attendee-sync path is untouched). Only title / description / start_time / end_time and
  deletions are gated.
- Externally **created** new events — only updates and deletions of events already present
  locally are gated; brand-new external events still sync in directly.
- Any change to the **outbound** local→GCal flow other than the reject / auto-undo writes
  introduced here.
- Approval of changes originating from providers other than Google Calendar (the model is
  provider-agnostic, but only the Google sync path is wired in v1).
- Expiry / auto-resolution of stale-but-unactioned pending requests (see Open Questions).

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Policy mechanism** | A single `TextChoices` field `external_event_update_policy` on `Organization` (`ALLOW` / `CHANGE_REQUEST` / `FORBIDDEN`), consistent with the project's per-tenant boolean settings (`should_sync_rooms`, `can_invite_organizations`). One field, not two booleans, because the three modes are mutually exclusive. |
| **Default value** | `CHANGE_REQUEST` from Phase 1 — the safe-by-default posture (the physician-edits-by-mistake case). The app is **pre-launch with no existing tenants**, so there is nothing to backfill or grandfather and no staged-activation phase: the field simply defaults to `CHANGE_REQUEST` and every new organization gets it. A tenant that wants direct apply opts into `ALLOW`. |
| **No rollout flag** | The policy field is permanent product config, not a temporary rollout flag, and there are no existing tenants/flows to protect — so no separate gating flag and no flag-removal phase. Phases still land in dependency order (interception code exists before the policy can route to it for any tenant), and `ALLOW`-mode tests prove direct-apply still works for tenants that choose it. |
| **Change-request storage** | New `ExternalEventChangeRequest` model in `calendar_integration`, an `OrganizationModel` subclass (tenant-scoped). Stores the target `CalendarEvent`, the change `kind` (`update` / `delete`), the proposed values + raw external payload, a snapshot of the retained (pre-change) local values, status, provider, and resolver membership + timestamp. |
| **One valid request per event** | At most one `PENDING` request per event. A new inbound external edit while one is pending marks the prior `PENDING` request `STALE` and creates a fresh `PENDING` one — history is preserved (never updated-in-place, never deleted). Enforced by a partial unique constraint on `(event, status=PENDING)`. |
| **Approver eligibility** | Member-attendees (an `EventAttendance` whose membership is the actor) may resolve requests on **their** events; admins (`OrganizationMembership.is_admin`) may resolve **any**. Eligibility lives in one reusable resolution-service method consumed by REST + GraphQL. |
| **Reject / FORBIDDEN convergence** | Both rejection and `FORBIDDEN` auto-undo push the retained value back to GCal: an inbound **update** is undone via `update_event`; an inbound **deletion** is undone by re-creating the event via `create_event` (the external id changes — the local event's `external_id` is updated to the new one). `FORBIDDEN` is effectively an immediate auto-reject performed during sync. |
| **Sync must not overwrite while gated** | Under `CHANGE_REQUEST`/`FORBIDDEN`, the sync diff engine never mutates or deletes the local event for gated changes, but still marks the external id as *matched* so full-sync deletion logic does not treat the event as vanished, and still advances the sync token so the change is not reprocessed indefinitely. |
| **API surface** | Internal REST (DRF, first-party frontend) **and** public GraphQL (external integrations). Both consume the same DI-injected resolution service. |
| **Audit** | `create`, `approve`, `reject`, and `forbidden`-auto-undo each emit an `AuditService.record(...)` call with a diff of proposed-vs-retained values. New `AuditAction` members added as needed. |
| **Notifications** | On request creation, dispatch an in-app notification (`NotificationService`, `NotificationTypes.IN_APP`) to every eligible approver (member-attendees + admins). Resolution notifications are out of scope for v1 (Open Questions). |

## 3. Data Model Changes

### 3.1 `Organization.external_event_update_policy`

New field on `Organization` in `@organizations/models.py` (alongside `should_sync_rooms` at
[organizations/models.py:92](../organizations/models.py#L92)):

```python
class ExternalEventUpdatePolicy(models.TextChoices):
    ALLOW = "allow", "Allow direct updates"
    CHANGE_REQUEST = "change_request", "Updates create change requests"
    FORBIDDEN = "forbidden", "Updates are forbidden"


# on Organization:
external_event_update_policy = models.CharField(
    max_length=20,
    choices=ExternalEventUpdatePolicy.choices,
    default=ExternalEventUpdatePolicy.CHANGE_REQUEST,
)
```

Define `ExternalEventUpdatePolicy` in `@organizations/models.py` (or a small `constants.py` if
the app gains one) and export it where the app exposes its enums. Register the field in
`@organizations/admin.py`.

### 3.2 New model `ExternalEventChangeRequest`

New model in `@calendar_integration/models.py`, an `OrganizationModel` subclass:

```python
class ExternalEventChangeKind(models.TextChoices):
    UPDATE = "update", "Update"
    DELETE = "delete", "Delete"


class ExternalEventChangeRequestStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    APPROVED = "approved", "Approved"
    REJECTED = "rejected", "Rejected"
    STALE = "stale", "Stale"          # superseded by a newer inbound edit
    AUTO_UNDONE = "auto_undone", "Auto-undone"  # FORBIDDEN mode


class ExternalEventChangeRequest(OrganizationModel):
    event = OrganizationForeignKey("CalendarEvent", related_name="external_change_requests", ...)
    kind = models.CharField(choices=ExternalEventChangeKind.choices, ...)
    status = models.CharField(
        choices=ExternalEventChangeRequestStatus.choices,
        default=ExternalEventChangeRequestStatus.PENDING,
    )
    provider = models.CharField(...)  # CalendarProvider; GOOGLE in v1

    # Proposed (incoming) state and the raw external payload that produced it.
    proposed_values = models.JSONField(default=dict)       # title/description/start_time/end_time
    proposed_payload = models.JSONField(default=dict)      # raw provider payload

    # Retained (pre-change) local snapshot used to undo on reject / FORBIDDEN.
    retained_values = models.JSONField(default=dict)

    resolved_by = OrganizationMembershipForeignKey(null=True, blank=True, ...)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            # at most one PENDING request per event
            models.UniqueConstraint(
                fields=["event"],
                condition=models.Q(status="pending"),
                name="uniq_pending_change_request_per_event",
            ),
        ]
```

Follow the [add-model](../.claude/skills/add-model) contract: custom manager + queryset, admin
registration in `@calendar_integration/admin.py`, factory in
`@calendar_integration/factories.py`, export from the app's model namespace. Membership FKs use
`OrganizationMembershipForeignKey` (composite `(organization_id, user_id)`), matching
`CalendarOwnership` / `EventAttendance`.

### 3.3 Type plumbing

- A small `dataclass`/`TypedDict` for the proposed-change payload passed from the sync diff
  engine to the change-request service (kind + proposed values + retained snapshot + raw
  payload), defined near `CalendarSyncService` in
  `@calendar_integration/services/calendar_sync_service.py`.
- New `AuditAction` members in `@audit/constants.py`:
  `calendar.event.external_change_requested`, `...approved`, `...rejected`,
  `...auto_undone` (names final at implementation time; follow the dotted convention already in
  the enum).

## 4. API Design

### 4.1 Internal REST — `ExternalEventChangeRequestViewSet`

Registered in `@calendar_integration/routes.py`, built on the project's
`VintaScheduleModelViewSet`, organization-scoped permissions, serializer-driven virtual model.

- `GET /change-requests/` — list `PENDING` (and optionally historical) requests the caller is
  eligible to see (their events if member; all if admin). Filterable by `event`, `status`.
- `POST /change-requests/{id}/approve/` — apply the proposed change locally.
- `POST /change-requests/{id}/reject/` — undo on GCal (push retained value / re-create on
  delete) and mark `REJECTED`.

Errors: `403` when the caller is not an eligible approver for that request; `409` when the
request is no longer `PENDING` (already resolved or stale).

### 4.2 Public GraphQL

In `@calendar_integration/graphql.py` + registered on `@public_api/queries.py` /
`@public_api/mutations.py`:
- Query: `externalEventChangeRequests` (eligibility-scoped, filterable).
- Mutations: `approveExternalEventChangeRequest(id)`, `rejectExternalEventChangeRequest(id)`.

Apply the project's auth + organization-scope permission classes and add the field to
`OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`. Business logic flows through the
DI-injected resolution service — no logic in the resolver.

## 5. Phased Rollout

Ordering: scaffolding first, then interception, then resolution + outbound undo, then
`FORBIDDEN`, then notifications, then API. The policy defaults to `CHANGE_REQUEST` from Phase 1;
because the app is pre-launch there are no tenants exercising the path until interception lands
in Phase 3, and no activation/backfill phase is needed. Phases remain independently mergeable —
the interception code (Phase 3+) exists before any tenant flow depends on it, and `ALLOW`-mode
tests prove direct apply still works for tenants that opt into it.

---

### Phase 1 — Add the `external_event_update_policy` field

**Goal**: `Organization` carries the policy field, defaulting to `CHANGE_REQUEST`. No
interception logic exists yet, so sync behavior is unchanged until Phase 3.

**Feature flag**: none — the field is itself the per-tenant policy; pre-launch, no flow depends
on it yet.

Changes:
1. `@organizations/models.py`: add `ExternalEventUpdatePolicy` TextChoices + the field
   (`default=CHANGE_REQUEST`).
2. Migration: `makemigrations organizations` — single column add with a constant default (safe,
   no table rewrite concern on Postgres for a `varchar` with default).
3. `@organizations/admin.py`: expose the field as an editable field + list filter/display.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `@organizations/tests/test_models.py` — field default is `CHANGE_REQUEST`; choices
  enforced.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`.
Single-field migration + enum, exact precedent at [organizations/models.py:92](../organizations/models.py#L92).

**Reusable skills**: `add-migration`.

Acceptance: a fresh `Organization` has `external_event_update_policy == "change_request"`;
migration applies and reverses cleanly; admin lets an operator switch a tenant's policy.

---

### Phase 2 — Add the `ExternalEventChangeRequest` model

**Goal**: the change-request table + manager + factory + admin exist; nothing writes to it yet.

**Feature flag**: none — new table no existing code reads/writes.

Changes:
1. `@calendar_integration/models.py`: `ExternalEventChangeKind`,
   `ExternalEventChangeRequestStatus`, `ExternalEventChangeRequest` (see Data Model Changes),
   including the partial unique constraint on `(event, status=PENDING)`.
2. Manager + queryset following the project's pattern; export from the model namespace.
3. `@calendar_integration/admin.py` registration; `@calendar_integration/factories.py` factory.
4. Migration via `makemigrations`.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `@calendar_integration/tests/models/test_external_event_change_request.py` — factory
  builds; partial unique constraint rejects a second `PENDING` row for the same event but allows
  a second non-`PENDING` row.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` if the
constraint migration needs hand-editing) / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `add-model`, `add-migration`.

Acceptance: model migrates, factory works, partial unique constraint enforced at the DB level.

---

### Phase 3 — Intercept inbound UPDATES into change requests

**Goal**: under `CHANGE_REQUEST`, an inbound field edit to an existing event creates (or
supersedes) a `PENDING` change request instead of mutating the local event.

**Feature flag**: gated by `external_event_update_policy`. When `ALLOW` (default pre-activation),
the existing direct-apply path in `_process_existing_event` runs byte-for-byte. When
`CHANGE_REQUEST`, the update is diverted.

Changes:
1. New `ExternalEventChangeRequestService` in
   `@calendar_integration/services/external_event_change_request_service.py`:
   `create_or_supersede_update_request(event, proposed_values, retained_values, payload)` —
   marks any existing `PENDING` request for the event `STALE`, creates a new `PENDING` one
   (transactional), records `AuditAction.calendar.event.external_change_requested`.
2. `@calendar_integration/services/calendar_sync_service.py`: in `_process_existing_event`
   ([calendar_sync_service.py:706-732](../calendar_integration/services/calendar_sync_service.py#L706-L732)),
   read the org policy; when not `ALLOW` and the incoming event is a (non-cancelled) edit, build
   the proposed/retained snapshot, route to the service, and **skip** appending to
   `changes.events_to_update`. Still add the external id to `changes.matched_event_ids` so
   full-sync deletion logic does not treat it as vanished.
3. Register the service in `@di_core/containers.py` (`providers.Factory`, injecting
   `audit_service`).

Spec use-case: external update under `CHANGE_REQUEST` → change request (incl. re-edit →
prior `PENDING` marked `STALE`, new `PENDING` created).

Tests:
- **Integration**: `@calendar_integration/tests/services/test_external_change_request_update.py`
  — a sync carrying an edited event under `CHANGE_REQUEST` leaves the local event untouched and
  creates one `PENDING` request; a second edit marks the first `STALE` and creates a new
  `PENDING`; the event is **not** deleted by full-sync.
- **Integration (flag-off)**: same edit under `ALLOW` applies directly (existing behavior
  unchanged) and creates no request.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches the
sync diff engine with branching + supersede semantics.

**Reusable skills**: `write-tests`.

Acceptance: under `CHANGE_REQUEST`, an inbound edit produces exactly one `PENDING` request, the
local event is unchanged, and re-edits supersede without losing history; under `ALLOW`, behavior
is identical to today.

---

### Phase 4 — Intercept inbound DELETIONS into change requests

**Goal**: under `CHANGE_REQUEST`, an inbound external deletion (`status == "cancelled"`) of an
existing event creates a `delete`-kind `PENDING` request instead of deleting locally.

**Feature flag**: gated by `external_event_update_policy`; `ALLOW` deletes directly (today's
behavior at [calendar_sync_service.py:717-720](../calendar_integration/services/calendar_sync_service.py#L717-L720)).

Changes:
1. `ExternalEventChangeRequestService.create_or_supersede_delete_request(event, retained_values, payload)`
   — same supersede semantics, `kind=DELETE`.
2. `@calendar_integration/services/calendar_sync_service.py`: in the cancelled-event branch,
   when policy is not `ALLOW`, route to the delete-request service and **skip** appending to
   `changes.events_to_delete`; keep the id in `matched_event_ids`.

Spec use-case: external deletion under `CHANGE_REQUEST` → delete change request.

Tests:
- **Integration**: `@calendar_integration/tests/services/test_external_change_request_delete.py`
  — a cancelled inbound event under `CHANGE_REQUEST` leaves the local event present and creates a
  `delete` `PENDING` request; under `ALLOW`, the event is deleted as today.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.

**Reusable skills**: `write-tests`.

Acceptance: under `CHANGE_REQUEST`, an inbound deletion keeps the local event and records a
`delete` request; under `ALLOW`, deletion still applies.

---

### Phase 5a — Approve a change request

**Goal**: an eligible approver applies a `PENDING` request — local event updated, or deleted for
`delete`-kind — marking it `APPROVED`.

**Feature flag**: none on the resolution action itself (requests only exist when a tenant is
non-`ALLOW`); eligibility enforced regardless.

Changes:
1. `ExternalEventChangeRequestService.approve(request, *, membership)`:
   - eligibility check `can_resolve(request, membership)` — member-attendee of the event
     (`EventAttendance`) or `membership.is_admin`;
   - `update` kind → write `proposed_values` onto the local `CalendarEvent`;
   - `delete` kind → delete the local `CalendarEvent`;
   - set `status=APPROVED`, `resolved_by`, `resolved_at`; record
     `AuditAction.calendar.event.external_change_approved`;
   - raise on non-`PENDING` (409) / ineligible (403).
2. Eligibility helper reused by the API phases.

Spec use-case: approve a change request.

Tests:
- **Integration**: `@calendar_integration/tests/services/test_change_request_approve.py` —
  member-attendee approves their event's update (local event reflects proposed values);
  admin approves any; `delete` approval removes the event; non-attendee non-admin is rejected;
  approving a non-`PENDING` request errors.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.

**Reusable skills**: `write-tests`.

Acceptance: approval applies the change for eligible approvers and is refused for everyone else.

---

### Phase 5b — Reject a change request (outbound undo)

**Goal**: an eligible approver rejects a `PENDING` request — the retained value is pushed back to
GCal (update undone, or event re-created for `delete`-kind) and the request is marked `REJECTED`.

**Feature flag**: none on the action (see Phase 5a).

Changes:
1. `ExternalEventChangeRequestService.reject(request, *, membership)`:
   - `update` kind → call the outbound update through `CalendarEventService` /
     `GoogleCalendarAdapter.update_event` ([google_calendar_adapter.py:492-547](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py#L492-L547))
     with `retained_values`, so GCal re-converges to the approved state;
   - `delete` kind → re-create on GCal via `create_event`
     ([google_calendar_adapter.py:279-353](../calendar_integration/services/calendar_adapters/google_calendar_adapter.py#L279-L353)),
     then update the local event's `external_id` to the newly returned id;
   - set `status=REJECTED`, `resolved_by`, `resolved_at`; record
     `AuditAction.calendar.event.external_change_rejected`.
2. Reuse the eligibility helper from Phase 5a.

Spec use-case: reject a change request.

Tests:
- **Integration**: `@calendar_integration/tests/services/test_change_request_reject.py` (mocked
  outbound adapter) — rejecting an `update` calls `update_event` with retained values; rejecting
  a `delete` calls `create_event` and rebinds the local `external_id`; eligibility + non-`PENDING`
  guards hold.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` / `gpt-5` (extended thinking) /
`gemini-3-pro`. Re-creating a deleted external event and rebinding the local external id is the
trickiest edge (external-id churn, partial-failure handling).

**Reusable skills**: `write-tests`.

Acceptance: rejecting an update re-converges GCal to the retained value; rejecting a deletion
re-creates the event on GCal and the local event tracks the new external id.

---

### Phase 6 — `FORBIDDEN` mode auto-undo during sync

**Goal**: under `FORBIDDEN`, inbound edits/deletions are undone immediately during sync (no
pending request lingers), reusing the Phase 5b outbound-undo machinery.

**Feature flag**: gated by `external_event_update_policy == FORBIDDEN`.

Changes:
1. `@calendar_integration/services/calendar_sync_service.py`: when policy is `FORBIDDEN`, instead
   of creating a `PENDING` request, invoke the outbound undo directly (update → push retained;
   delete → re-create) and record an `AUTO_UNDONE` request row for history +
   `AuditAction.calendar.event.external_change_auto_undone`.
2. Factor the outbound-undo body from Phase 5b into a shared service method so reject and
   `FORBIDDEN` share one code path.

Spec use-case: `FORBIDDEN` auto-undo of inbound update and of inbound deletion.

Tests:
- **Integration**: `@calendar_integration/tests/services/test_forbidden_auto_undo.py` (mocked
  outbound) — an inbound edit under `FORBIDDEN` triggers `update_event` with retained values and
  records an `AUTO_UNDONE` row; an inbound deletion triggers `create_event` and rebinds the
  external id; no `PENDING` request remains.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro` (reuses Phase
5b primitives).

**Reusable skills**: `write-tests`.

Acceptance: under `FORBIDDEN`, every inbound edit/deletion is auto-undone on GCal within the sync
and leaves an `AUTO_UNDONE` audit-visible record; no approval is required.

---

### Phase 7 — Notify eligible approvers on request creation

**Goal**: when a `PENDING` request is created under `CHANGE_REQUEST`, each eligible approver
receives an in-app notification.

**Feature flag**: implicitly gated — only fires when a request is created (non-`ALLOW` tenants).

Changes:
1. `ExternalEventChangeRequestService`: after creating a `PENDING` request, resolve eligible
   approvers (member-attendees of the event + organization admins) and dispatch an in-app
   notification via `NotificationService` (`NotificationTypes.IN_APP`), wrapped in
   `transaction.on_commit`.
2. Notification context + template per the notifications app conventions.

Spec use-case: notify approvers of a pending external change.

Tests:
- **Integration**: `@calendar_integration/tests/services/test_change_request_notifications.py`
  (mocked `NotificationService`) — creating a request dispatches one in-app notification per
  eligible approver and none to ineligible members.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `write-tests`.

Acceptance: request creation notifies exactly the eligible approvers in-app.

---

### Phase 8a — REST endpoints (list / approve / reject)

**Goal**: the first-party frontend can list pending requests and approve/reject them.

**Feature flag**: none — eligibility-scoped endpoints; harmless when no requests exist.

Changes:
1. `ExternalEventChangeRequestViewSet` + serializer + filterset, registered in
   `@calendar_integration/routes.py`; eligibility-scoped queryset; `approve`/`reject` actions
   delegate to the service.
2. Regenerate `schema.yml`.

Spec use-case: list + act on change requests via REST.

Tests:
- **Integration**: `@calendar_integration/tests/test_change_request_api.py` — list returns only
  eligible requests; approve/reject delegate correctly; `403` for ineligible, `409` for
  non-`PENDING`.

> Backend/API only (no browser surface in this repo — the frontend is a separate app), so no
> Playwright E2E per the plan's E2E rule.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.

**Reusable skills**: `create-rest-endpoint`, `write-tests`.

Acceptance: REST list/approve/reject work end-to-end with eligibility + status guards;
`schema.yml` regenerated.

---

### Phase 8b — Public GraphQL query + mutations

**Goal**: external integrations can list pending requests and approve/reject them.

**Feature flag**: none (see Phase 8a).

Changes:
1. `@calendar_integration/graphql.py`: strawberry-django type + query + `approve`/`reject`
   mutations; register on `@public_api/queries.py` / `@public_api/mutations.py`; add to
   `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`; apply auth + org-scope permissions.
   Logic via the DI-injected service.

Spec use-case: list + act on change requests via public GraphQL.

Tests:
- **Integration**: `@public_api/tests/test_external_change_request_graphql.py` — query is
  eligibility-scoped; mutations delegate to the service; permission classes enforced.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`.

**Reusable skills**: `create-graphql-public-query`, `write-tests`.

Acceptance: GraphQL query + mutations behave identically to REST, with public-API auth + scope
enforced.

## 6. Risk & Rollout Notes

- **Pre-launch, so no backfill/activation risk.** With no existing tenants, the field simply
  defaults to `CHANGE_REQUEST` from Phase 1; there is no behavior-change-on-deploy for live
  tenants and no staged flip. The main correctness risks are the sync-interception edges below.
- **Sync token / reprocessing.** Under `CHANGE_REQUEST`/`FORBIDDEN`, gated changes do not mutate
  the local event but the sync **must still advance the sync token** and mark the external id as
  *matched* — otherwise full-sync deletion logic
  ([calendar_sync_service.py:934-952](../calendar_integration/services/calendar_sync_service.py#L934-L952))
  could delete the retained event, or the change could be reprocessed. Covered by Phase 3/4
  acceptance.
- **External-id churn on reject/auto-undo of deletions.** Re-creating a deleted event yields a
  **new** GCal external id; the local event's `external_id` must be rebound atomically. Partial
  failure (re-create succeeds, local save fails) must not orphan the local event — wrap in a
  transaction and treat the adapter call as the commit boundary. This is the riskiest mechanic
  (Phase 5b, Tier 4).
- **Migrations.** Phase 1 adds a `varchar` with a constant default (no rewrite concern on
  Postgres for added columns with defaults). Phase 2 adds a new table + partial unique index.
  No data backfill migration is needed (pre-launch).
- **Idempotency / supersede races.** Two near-simultaneous inbound edits could race on the
  "mark prior `PENDING` stale + create new" step; the partial unique constraint on
  `(event, status=PENDING)` is the backstop (second insert fails → retry/supersede). Do the
  stale+create inside one transaction.
- **Audit volume.** `FORBIDDEN` tenants generate an audit + `AUTO_UNDONE` row per inbound edit;
  acceptable, but worth a dashboard if a tenant's GCal is noisy.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should approvers/requesters be notified on **resolution** (approve/reject), not just creation? | No in v1; add in a follow-up once the in-app surface exists. | Product |
| Do pending requests **expire** if unactioned for N days? | No expiry in v1; revisit if backlogs appear. | Product |
| Should `FORBIDDEN` auto-undo also notify admins (so they know GCal edits are being reverted)? | No in v1; the `AUTO_UNDONE` audit rows are the record. | Product |
| Are attendee/RSVP inbound changes ever gated in a later version? | Out of scope v1; reassess after the field-edit workflow lands. | Product |
| Should the REST/GraphQL list expose **historical** (`stale`/`resolved`) requests or only `PENDING`? | `PENDING` by default, history behind an explicit `status` filter. | Eng |

## 8. Touch List

**Phase 1**
- edit `@organizations/models.py` (field + `ExternalEventUpdatePolicy`, `default=CHANGE_REQUEST`)
- edit `@organizations/admin.py` (editable field + list filter/display)
- new migration `organizations/migrations/`
- edit [organizations/tests/test_models.py](../organizations/tests/test_models.py)

**Phase 2**
- edit `@calendar_integration/models.py` (model + enums + constraint)
- edit `@calendar_integration/admin.py`, `@calendar_integration/factories.py`
- new migration `calendar_integration/migrations/`
- new `@calendar_integration/tests/models/test_external_event_change_request.py`

**Phase 3**
- new `@calendar_integration/services/external_event_change_request_service.py`
- edit `@calendar_integration/services/calendar_sync_service.py` (update interception)
- edit `@di_core/containers.py`
- edit `@audit/constants.py` (new actions)
- new `@calendar_integration/tests/services/test_external_change_request_update.py`

**Phase 4**
- edit `@calendar_integration/services/calendar_sync_service.py` (delete interception)
- edit `@calendar_integration/services/external_event_change_request_service.py`
- new `@calendar_integration/tests/services/test_external_change_request_delete.py`

**Phase 5a**
- edit `@calendar_integration/services/external_event_change_request_service.py` (approve +
  eligibility helper)
- new `@calendar_integration/tests/services/test_change_request_approve.py`

**Phase 5b**
- edit `@calendar_integration/services/external_event_change_request_service.py` (reject +
  outbound undo)
- new `@calendar_integration/tests/services/test_change_request_reject.py`

**Phase 6**
- edit `@calendar_integration/services/calendar_sync_service.py` (`FORBIDDEN` path)
- edit `@calendar_integration/services/external_event_change_request_service.py` (shared
  auto-undo)
- new `@calendar_integration/tests/services/test_forbidden_auto_undo.py`

**Phase 7**
- edit `@calendar_integration/services/external_event_change_request_service.py` (notify)
- new notification context/template per the notifications app
- new `@calendar_integration/tests/services/test_change_request_notifications.py`

**Phase 8a**
- new viewset + serializer + filterset in `@calendar_integration/` ; edit
  `@calendar_integration/routes.py`
- edit `schema.yml`
- new `@calendar_integration/tests/test_change_request_api.py`

**Phase 8b**
- edit `@calendar_integration/graphql.py`, `@public_api/queries.py`, `@public_api/mutations.py`,
  `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`
- new `@public_api/tests/test_external_change_request_graphql.py`
