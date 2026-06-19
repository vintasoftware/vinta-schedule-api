# organization_member_created Outgoing Webhook — Implementation Plan

Spec sibling: [2026-06-17-ORGANIZATION_MEMBER_CREATED_WEBHOOK_SPEC.md](2026-06-17-ORGANIZATION_MEMBER_CREATED_WEBHOOK_SPEC.md). This plan translates that spec into phased delivery; it does not re-derive requirements. Read the spec's **Decisions** and **Risks assumed** first.

## 1. Goals

1. Add a new outgoing webhook event type `organization_member_created` that fires once per **active** `OrganizationMembership` creation, scoped to that membership's organization.
2. Deliver a payload — wrapped in a new envelope `{id, type, timestamp, data}` — carrying the integer Vinta user id, email, organization id + name, and membership role + id, so a Medplum bot can create/link a Provider idempotently.
3. Switch **all** event types (calendar events included) to the enveloped delivery shape, with an envelope `id` stable across a logical event's retry chain.
4. Expose webhook-configuration management over the public GraphQL API at full CRUD parity with the existing REST surface, organization-scoped, plus read access to delivery history.

**Non-goals** (carried from the spec's **Negative scope**):

- Per-user / patient-scoped tokens and single-use scheduling codes — separate, independently planned consumers of this webhook.
- Inbound Medplum→Vinta writeback of the minted token / Provider id.
- Backfill of `organization_member_created` for memberships that already exist before this ships.
- HMAC / signature verification of payloads.
- `organization_member_updated` / `organization_member_deleted` events.
- Profile (display) name in the payload (see **Open Questions**).
- A new external UUID/public identifier for users or organizations — the integer pk is the linking key.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Event-type constant** | `WebhookEventType.ORGANIZATION_MEMBER_CREATED = "organization_member_created"`. Trigger is membership creation (the only point with an org to scope an org-scoped webhook to); named for what literally fires. |
| **Trigger mechanism** | **Explicit side-effect service calls** (mirroring `WebhookCalendarEventSideEffectsService`), wired into each `OrganizationMembership` creation site in `organizations/services.py`. Chosen over a `post_save` signal for consistency with the existing webhook pattern and greppability — at the cost of having to wire every creation site (and guard against double-emission across overlapping service paths). |
| **Active gate** | Emit only when the created membership has `is_active=True`. Gated/inactive memberships emit nothing. |
| **Linking identifier** | Integer `User.id` + `Organization.id`. No new identifier column; reuses the precedent that calendar payloads send integer ids. |
| **Payload `data`** | `user_id`, `email`, `organization_id`, `organization_name`, `membership_role`, `membership_id`. No profile name (Open Question). |
| **Envelope** | `{id, type, timestamp, data}` constructed at **delivery time** in `process_webhook_event`, applied to every event type. `id = event.main_event_id or event.id` so it is stable across a logical event's retry chain. Built at delivery (not stored) because the retry-chain root id is needed. |
| **Envelope rollout** | **Hard unflagged cutover** — no feature flag (explicit stakeholder decision). All event types switch to the enveloped shape at deploy; rollback is a code revert. Breaking change to existing calendar consumers is accepted; coordination is a **Risk & Rollout** item. |
| **Idempotency** | Reuse the existing at-least-once delivery + exponential backoff (existing max retries). No dedup on Vinta's side; the bot dedupes on envelope `id`. No new fields on `WebhookEvent`. |
| **GraphQL surface** | Full CRUD parity for `WebhookConfiguration` + read access to `WebhookEvent` history, over `public_api`, org-scoped via `IsAuthenticated` + `OrganizationResourceAccess`. REST surface stays unchanged. Additive new surface — no flag. Ships **after** the event itself works. |
| **Feature flag** | **None.** The envelope cutover is a deliberate hard break (justified above); the new event type and the GraphQL surface are purely additive (no existing caller hits them). No flag-removal phase. |
| **Migration** | No schema change. Adding a `TextChoices` member generates a cosmetic `AlterField` (choices metadata) migration with no DB-level effect. |

## 3. Data Model Changes

No new tables, no new columns. `WebhookConfiguration` and `WebhookEvent` (in @webhooks/models.py) are reused as-is.

### 3.1 Event-type constant

Add one member to `WebhookEventType` in @webhooks/constants.py:

```python
class WebhookEventType(models.TextChoices):
    # ... existing calendar members ...
    ORGANIZATION_MEMBER_CREATED = "organization_member_created", "Organization member created"
```

A `makemigrations` run will emit a no-op `AlterField` on `WebhookConfiguration.event_type` and `WebhookEvent.event_type` for the changed choices — include it; it does not touch data.

### 3.2 Payload type

Add a `TypedDict` beside the existing payload types in @webhooks/services/payloads.py:

```python
class OrganizationMemberCreatedWebhookPayload(TypedDict):
    user_id: int
    email: str
    organization_id: int
    organization_name: str
    membership_role: str
    membership_id: int
```

### 3.3 Envelope type

Add an envelope `TypedDict` (used by the delivery layer to document the wrapper shape) in @webhooks/services/payloads.py:

```python
class WebhookEnvelope(TypedDict):
    id: str
    type: str
    timestamp: str  # ISO 8601
    data: dict
```

### 3.4 Public-API resource constant

For the GraphQL phases, add `WEBHOOK_CONFIGURATION` to the `PublicAPIResources` enum used by `OrganizationResourceAccess` (resource lives in the public_api permissions module referenced by [public_api/permissions FIELD_TO_RESOURCE_MAPPING](../public_api/mutations.py)). Plumbed in the GraphQL foundation phase, not earlier.

## 4. API Design

### 4.1 Delivery envelope (all event types)

At delivery time `process_webhook_event` wraps the stored `payload` dict:

```
POST {configuration.url}
{
  "id":   "<main_event_id or event_id>",
  "type": "<event_type>",
  "timestamp": "<event.created ISO8601>",
  "data": { ...the stored payload dict... }
}
```

`id` is the retry-chain root id, so all retries of one logical event share it.

### 4.2 GraphQL — WebhookConfiguration CRUD + WebhookEvent read

Mirrors the existing REST contract (`/webhook-configurations/`, `/webhook-events/`), org-scoped:

- `webhookConfigurations` query — list/read configs for the caller's organization.
- `createWebhookConfiguration(input)` — `{ event_type, url, headers }` → created config.
- `updateWebhookConfiguration(input)` — partial update by id, org-scoped.
- `deleteWebhookConfiguration(input)` — soft-delete by id (reuse `webhook_service.delete_configuration`).
- `webhookEvents` query — read delivery history, org-scoped, read-only.

All fields registered in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` against `PublicAPIResources.WEBHOOK_CONFIGURATION`, guarded by `IsAuthenticated` + `OrganizationResourceAccess`.

## 5. Phased Rollout

Ordering rationale: the envelope is shared delivery infra and the breaking change — land it first so the new event and all existing events ship one consistent shape. The event-emission use-cases land next (the Medplum-facing value). GraphQL config management is additive and lands last; the webhook fires for Medplum without it.

### Phase 0 — Add event type + payload/envelope types

**Goal**: Scaffolding — the `organization_member_created` event type and its payload/envelope types exist and are selectable on a configuration. No event fires yet. Ship value: none on its own; every later phase consumes these types.

**Feature flag**: none — pure additive scaffolding.

Changes:
1. @webhooks/constants.py: add `ORGANIZATION_MEMBER_CREATED` to `WebhookEventType`.
2. @webhooks/services/payloads.py: add `OrganizationMemberCreatedWebhookPayload` and `WebhookEnvelope` TypedDicts.
3. Generated migration for the `event_type` choices `AlterField` (no-op DB).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @webhooks/tests/test_constants.py — `ORGANIZATION_MEMBER_CREATED` present with value `"organization_member_created"`.
- **Integration**: @webhooks/tests/test_views.py — a `WebhookConfiguration` can be created for the new event type over REST and round-trips.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Enum member + TypedDicts + cosmetic migration; exact precedent.

**Reusable skills**: `add-migration` (for the choices `AlterField`).

Acceptance: the new event type is a valid configuration choice and round-trips through REST create/read; full suite green.

### Phase 1 — Enveloped delivery for all event types (breaking)

**Goal**: Every webhook delivery POSTs `{id, type, timestamp, data}` instead of the raw payload, with `id` stable across a logical event's retry chain. Producer-visible outcome: consumers receive the new shape.

**Feature flag**: none — deliberate hard cutover (see **Guiding Decisions**). Document the breaking change in the PR description; coordinate per **Risk & Rollout Notes**.

Changes:
1. @webhooks/services/webhook_service.py: in `process_webhook_event`, build the envelope before `requests.post(...)` ([webhook_service.py:134](../webhooks/services/webhook_service.py#L134)) — `id = event.main_event_id or event.id`, `type = event.event_type`, `timestamp = event.created` ISO8601, `data = event.payload`. POST the envelope as `json=`.
2. Keep `WebhookEvent.payload` storing only the `data` dict (envelope is delivery-time only) so the retry-chain root id resolves correctly on re-delivery.

Spec use-case: shared scaffolding — no use-case yet (enables every event type, including the new one).

Tests:
- **Unit**: @webhooks/tests/test_services.py — envelope shape for a delivery; `id == main_event_id` for a retry and `id == event.id` for a first attempt; `timestamp` is ISO8601; `data` equals the stored payload.
- **Integration**: @webhooks/tests/test_services.py — a calendar-event delivery (existing event type) now produces the enveloped body; a retry of one event reuses the same `id` across the chain.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches delivery + retry/idempotency semantics shared by all event types.

**Reusable skills**: `write-tests`.

Acceptance: every delivered webhook (new and existing event types) is enveloped; retries of one logical event share one envelope `id`; existing delivery/retry tests updated to the new shape and green.

### Phase 2 — Membership side-effects service + invitation-accept emission

**Goal**: When a user accepts an organization invitation and an active membership is created, one `organization_member_created` event is emitted per subscribed configuration. First Medplum-facing value.

**Feature flag**: none — additive new event; no existing consumer subscribes unless a config is created.

Changes:
1. New @webhooks/services/webhook_membership_side_effects.py: `WebhookMembershipSideEffectsService` with `@inject` `webhook_service` (mirror [webhook_calendar_side_effects.py:22-63](../webhooks/services/webhook_calendar_side_effects.py#L22-L63)); method `on_member_created(membership)` that returns early unless `membership.is_active`, serializes the `OrganizationMemberCreatedWebhookPayload`, and calls `webhook_service.send_event(organization=..., event_type=ORGANIZATION_MEMBER_CREATED, payload=...)`.
2. @di_core/containers.py: register `webhook_membership_side_effects_service = providers.Factory(WebhookMembershipSideEffectsService, webhook_service=webhook_service)` ([containers.py:85-92](../di_core/containers.py#L85-L92)).
3. @organizations/services.py: inject the side-effects service into `OrganizationService` and call `on_member_created(membership)` after the membership is created in `accept_invitation` ([services.py:303-305](../organizations/services.py#L303-L305)).

Spec use-case: "A user accepts an invitation and the Medplum bot links a Provider."

Tests:
- **Unit**: @webhooks/tests/test_membership_side_effects.py — `on_member_created` emits for active membership; no-op for inactive; payload field-for-field matches the contract.
- **Integration**: @organizations/tests/test_services.py — accepting an invitation creates one delivery per subscribed config, scoped to the right org, with the member-role payload; no delivery when no config subscribes.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New service + DI wiring + emission into an existing multi-branch flow.

**Reusable skills**: `write-tests`.

Acceptance: accepting an invitation that creates an active membership emits exactly one enveloped `organization_member_created` per subscribed config in that org, with the correct payload; inactive membership emits nothing.

### Phase 3 — Org-creator (admin) emission

**Goal**: When a user creates a new organization and becomes its active admin, the same event fires with `membership_role = admin`.

**Feature flag**: none — same additive event.

Changes:
1. @organizations/services.py: call `on_member_created(membership)` after the admin membership is created in `create_organization` ([services.py:72-76](../organizations/services.py#L72-L76)).

Spec use-case: "A user creates a brand-new organization."

Tests:
- **Integration**: @organizations/tests/test_services.py — creating an organization emits one delivery with `membership_role = "admin"` for the new org's subscribed configs.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (iterate) / `gpt-5-mini` / `gemini-2.5-flash`. Single call site + one test; reuses Phase 2 machinery.

**Reusable skills**: `write-tests`.

Acceptance: organization creation emits one `organization_member_created` with the admin role for that org only.

### Phase 4 — Provision-path coverage + multi-org refire

**Goal**: The remaining membership-creation site (`provision_tenant_for_user`) emits, and an existing user joining a second organization refires the event scoped to that second org only. Closes the firing-rule gaps.

**Feature flag**: none.

Changes:
1. @organizations/services.py: ensure `on_member_created(membership)` fires for the membership created in `provision_tenant_for_user` ([services.py:368-372](../organizations/services.py#L368-L372)) **without double-emitting** when that method delegates to `accept_invitation` (wired in Phase 2). Emit at the leaf creation only; add a guard/assert that each logical join emits exactly once.

Spec use-case: "An existing user joins a second organization."

Tests:
- **Integration**: @organizations/tests/test_services.py — a user already active in org A who gains an active membership in org B produces exactly one delivery, to B's configs only, none to A; the provision path emits exactly once (no duplicate across `provision_tenant_for_user`→`accept_invitation`).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Double-emission guard across overlapping service paths is the subtle part.

**Reusable skills**: `write-tests`.

Acceptance: every active-membership creation path emits exactly once; second-org join is scoped to the second org; no path double-fires.

### Phase 5 — GraphQL foundation: resource + WebhookConfiguration type

**Goal**: Scaffolding for GraphQL config management — the `WebhookConfiguration` GraphQL type and the `WEBHOOK_CONFIGURATION` resource exist; no query/mutation wired yet.

**Feature flag**: none — additive new surface.

Changes:
1. Public-API permissions module: add `PublicAPIResources.WEBHOOK_CONFIGURATION`.
2. New @webhooks/graphql.py: `strawberry_django` type for `WebhookConfiguration` (fields `id, event_type, url, headers`) and a read type for `WebhookEvent` history (mirror [organizations/graphql.py:1-17](../organizations/graphql.py#L1-L17)).

Spec use-case: shared scaffolding for the GraphQL use-case.

Tests:
- **Unit**: @webhooks/tests/test_graphql.py — the type resolves the expected fields against a baked `WebhookConfiguration`.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Type definition + enum constant; exact precedent.

**Reusable skills**: `create-graphql-public-query`.

Acceptance: the GraphQL type and resource constant exist and resolve; no behavior change to REST.

### Phase 6 — GraphQL WebhookConfiguration CRUD

**Goal**: External integrations can create, read, update, and delete webhook configurations over the public GraphQL API, organization-scoped.

**Feature flag**: none — additive new surface.

Changes:
1. @public_api/queries.py: register `webhookConfigurations` query, org-scoped, in `FIELD_TO_RESOURCE_MAPPING` → `WEBHOOK_CONFIGURATION`.
2. @public_api/mutations.py: `createWebhookConfiguration` / `updateWebhookConfiguration` / `deleteWebhookConfiguration` (delete reuses `webhook_service.delete_configuration` soft-delete), each `permission_classes=[IsAuthenticated, OrganizationResourceAccess]`, scoped to `info.context.request.public_api_organization` (mirror [public_api/mutations.py:75-103](../public_api/mutations.py#L75-L103)).

Spec use-case: "Integration self-manages its webhook subscription over GraphQL."

Tests:
- **Integration**: @public_api/tests/test_webhook_graphql.py — create/update/delete a config via GraphQL is org-scoped; a caller cannot read or mutate another org's config (tenant-isolation assertion); created config delivers on the next matching event.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Multi-mutation wiring + permission/tenant-isolation correctness.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: GraphQL CRUD on `WebhookConfiguration` matches REST capability, is org-scoped, and rejects cross-org access.

### Phase 7 — GraphQL WebhookEvent history read

**Goal**: Integrations can read delivery history over GraphQL (read-only), completing CRUD-parity-plus-history.

**Feature flag**: none — additive new surface.

Changes:
1. @public_api/queries.py: register `webhookEvents` read-only query, org-scoped, in `FIELD_TO_RESOURCE_MAPPING` → `WEBHOOK_CONFIGURATION`.

Spec use-case: "Integration self-manages its webhook subscription over GraphQL" (history-read portion).

Tests:
- **Integration**: @public_api/tests/test_webhook_graphql.py — `webhookEvents` returns only the caller org's events; no write path is exposed.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (iterate) / `gpt-5-mini` / `gemini-2.5-flash`. Single read query mirroring Phase 6 scoping.

**Reusable skills**: `create-graphql-public-query`; `write-tests`.

Acceptance: delivery history is readable over GraphQL, org-scoped, read-only; full suite green.

## 6. Risk & Rollout Notes

- **No feature flag — deliberate.** The envelope (Phase 1) is a hard breaking cutover by stakeholder decision; rollback is a code revert + redeploy, not a flag flip. Phases 2–7 are additive (new event type, new GraphQL surface) and need no flag.
- **Breaking change to existing calendar webhook consumers (Phase 1).** Any live consumer parsing the raw calendar payload breaks the moment Phase 1 deploys. Before merging Phase 1: inventory active `WebhookConfiguration` rows and their endpoints, notify owners, and stage on a non-production environment first. This is the one-way door — sequence its deploy with consumer coordination.
- **Double-emission risk (Phase 4).** `provision_tenant_for_user` may delegate to `accept_invitation`; wiring emission into both without care fires twice for one logical join. Emit at the leaf creation only; the Phase 4 test asserts exactly-once.
- **Migration.** Choices-only `AlterField` — metadata, no locks, no rewrite, instant. No hot-table risk.
- **No backfill.** Pre-existing memberships never emit. If Medplum needs them, that is a separate one-off script (`add-one-off-script`), explicitly out of scope.
- **At-least-once delivery.** Duplicate Provider creation if the bot ignores the envelope `id`; the contract is documented for the bot author but not enforceable from Vinta. Covered by the spec's **Risks assumed**.
- **GraphQL tenant isolation.** Reusing `OrganizationResourceAccess` must scope every new field; Phase 6/7 tests assert cross-org access is rejected. A mis-wired field would leak another org's config — high severity, covered by tests.
- **Rollback story.** Phases 0, 2–7 are additive and revertible with no data effect. Phase 1 rollback requires reverting the delivery code (and any consumer that already adapted to the envelope must revert too) — treat as the coordinated, irreversible step.

## 7. Open Questions

1. **Profile (display) name in the payload?** Recommended default: include `first_name` + `last_name` so the bot can name the Provider. Owner: Medplum integration owner. Currently excluded per spec; if added, it is a one-line payload extension in Phase 2 before that phase merges.
2. **Inactive→active transition emits?** Recommended default: emit when a gated membership first becomes active. Owner: membership-lifecycle product owner. If yes, add a small phase wiring emission into the activation path (currently only creation-of-active fires).
3. **Member updated/removed events soon?** Recommended default: defer. Owner: Medplum integration owner. Affects whether the event-type set is designed for extension now.
4. **Integer id exposure acceptable?** Recommended default: yes — endpoint is a trusted org-scoped integration. Owner: security reviewer. If not, a stable opaque identifier becomes a prerequisite phase.

## 8. Touch List

**Phase 0**
- @webhooks/constants.py (edit) — add event-type member.
- @webhooks/services/payloads.py (edit) — add payload + envelope TypedDicts.
- @webhooks/migrations/ (new) — choices `AlterField`.
- @webhooks/tests/test_constants.py (new/edit), @webhooks/tests/test_views.py (edit).

**Phase 1**
- [webhooks/services/webhook_service.py](../webhooks/services/webhook_service.py#L122-L162) (edit) — build envelope at delivery.
- @webhooks/tests/test_services.py (edit) — envelope shape, stable retry id, existing-event-type deliveries.

**Phase 2**
- @webhooks/services/webhook_membership_side_effects.py (new).
- [di_core/containers.py](../di_core/containers.py#L85-L92) (edit) — register service.
- [organizations/services.py](../organizations/services.py#L303-L305) (edit) — emit in `accept_invitation`; inject service into `OrganizationService`.
- @webhooks/tests/test_membership_side_effects.py (new), @organizations/tests/test_services.py (edit).

**Phase 3**
- [organizations/services.py](../organizations/services.py#L72-L76) (edit) — emit in `create_organization`.
- @organizations/tests/test_services.py (edit).

**Phase 4**
- [organizations/services.py](../organizations/services.py#L368-L372) (edit) — emit in `provision_tenant_for_user`; exactly-once guard.
- @organizations/tests/test_services.py (edit).

**Phase 5**
- Public-API permissions module (edit) — add `WEBHOOK_CONFIGURATION` resource.
- @webhooks/graphql.py (new) — config + event read types.
- @webhooks/tests/test_graphql.py (new).

**Phase 6**
- [public_api/queries.py](../public_api/queries.py) (edit) — `webhookConfigurations` query + mapping.
- [public_api/mutations.py](../public_api/mutations.py) (edit) — create/update/delete mutations + mapping.
- @public_api/tests/test_webhook_graphql.py (new).

**Phase 7**
- [public_api/queries.py](../public_api/queries.py) (edit) — `webhookEvents` read query + mapping.
- @public_api/tests/test_webhook_graphql.py (edit).
