# Per-Owner-Scoped Public API Tokens — Implementation Plan

Spec: [2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md](2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md). This plan translates that spec into phased delivery; it does not re-derive requirements. Read the spec first.

## 1. Goals

1. A Public API token can be scoped to exactly one provider (`SystemUser.scoped_to_user`); a scoped token can only ever read or write data belonging to calendars that provider owns.
2. Existing org-wide tokens (`scoped_to_user IS NULL`) behave identically to today, byte-for-byte, with no data backfill.
3. The Medplum bot can mint a provider-scoped token through a new `createScopedSystemUser` GraphQL mutation, and an org admin can mint one through the existing REST create endpoint (optional owner field).
4. The provider write capabilities the scoped token needs — manage recurring availability, add specific availability dates, add blocked times, schedule events — exist as public GraphQL mutations, each owner-guarded.
5. The scope cannot be bypassed: every reachable field and mutation filters or guards by owner, confirmed by an adversarial test sweep and a security-review checklist.

**Non-goals:**
- Patient tokens — deferred entirely until the single-use scheduling-code work lands (see **Open Questions**). No patient resource set, no patient mutation, nothing patient-shaped ships here.
- Single-use scheduling / rescheduling / cancelling codes — separate, independently planned.
- The `user_created` outgoing webhook that triggers the bot — separate, independently planned.
- Provider/practitioner user creation itself — assumed to already exist before minting.
- Mutable owner / re-scoping a token in place — owner is immutable; re-scope = revoke + re-mint.
- New `PublicAPIResources` enum values — provider capabilities reuse existing resources.
- Object-level / per-resource-different-owner scoping; cross-organization scoping.
- A feature-flag module — the change is data-gated (null owner), so no flag infra is introduced.
- CalendarGroup-pool scoping.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Scope storage** | **Amended 2026-06-18**: new nullable `SystemUser.scoped_to_membership` `OrganizationForeignKey` to `organizations.OrganizationMembership` (was a plain FK to `users.User`). A membership is the unique `(user, org)` pair, so the scope is org-bound at the schema level and the FK can be an `OrganizationForeignKey` (which `users.User` cannot, having no `organization_id`). `NULL` = today's org-wide token. Single nullable column, clean backward-compat, zero backfill. **External API stays user-centric**: callers pass/receive `scoped_to_user_id` (a `User` id); mint resolves the user's active membership and stores it. |
| **Owner edge** | "This provider's data" = calendars where `CalendarOwnership.user` is the membership's user (related_name `ownerships` on `Calendar`), and the events / blocked times / available times on those calendars. The helper joins `ownerships__user__organization_memberships = scoped_to_membership_id`. |
| **Enforcement location** | Two layers. The permission class still gates the *resource type*. A shared helper derives the owner's calendar-id set; every read resolver intersects its queryset against it, and every write mutation refuses a target calendar not in it. Filtering lives where the queryset is built (not in the boolean permission class) because list queries without a `calendarId` are the leak-prone cases. |
| **Cross-owner response** | Reads return empty; writes return not-found. Never confirms the existence of another provider's object. |
| **Rollout gating** | No feature flag (repo has no flag infra). Enforcement only activates when `scoped_to_user` is non-null, so null-owner tokens are provably unchanged. Every enforcement phase ships a test asserting an org-wide token sees identical behavior. No flag-removal phase. |
| **Mint surface** | New `createScopedSystemUser` GraphQL mutation (one parameterized mutation: owner optional + resource set), guarded by `IsAuthenticated` + `OrganizationResourceAccess` mapped to `SYSTEM_USER`. REST `POST /public-api-tokens/` additionally accepts an optional owner; the no-owner REST path is held byte-for-byte. |
| **Owner reference** | The mutation/endpoint takes our internal `User` id. It must resolve to a user in the caller's organization or the mint is rejected — a token can never be scoped cross-org. |
| **Owner immutability** | `scoped_to_user` is set at creation only. The REST grant-edit path (`update`/`partial_update`) reconciles resource grants but never touches the owner. |
| **Mint resource validation** | Fixed allow-list per shape. The provider shape may only grant the provider resource set (`AVAILABLE_TIME`, `BLOCKED_TIME`, `CALENDAR_EVENT`, `AVAILABILITY_WINDOWS`, `UNAVAILABLE_WINDOWS`, `CALENDAR`). Over-granting is rejected at mint time. |
| **Resource enums** | Reuse existing `PublicAPIResources`. Recurring vs specific availability are both `AVAILABLE_TIME` (the rrule distinguishes them). No new enum values, no enum migration. |
| **Deactivated owner** | No cascade job. The owner's calendar set is re-derived per request, so a deactivated provider naturally yields an empty accessible set (reads empty, writes not-found). |
| **Mint idempotency** | `integration_name` stays unique; a duplicate mint is rejected (no secret re-issued). The bot uses a deterministic name per provider and treats "already exists" as already-provisioned. |

## 3. Data Model Changes

### 3.1 `SystemUser.scoped_to_membership`

> **Amended 2026-06-18**: the scope FK targets `organizations.OrganizationMembership` via an
> `OrganizationForeignKey`, not `users.User`. A membership is the `(user, organization)` pair (unique),
> so the FK is inherently org-bound and `OrganizationForeignKey` adds `organization_id` to the JOIN ON
> clause (schema-level tenant safety). `User` has no `organization_id` column, so it cannot be an
> `OrganizationForeignKey` target. The **external API contract is unchanged** — callers still pass and
> receive a `scoped_to_user_id` (an internal `User` id); mint resolves it to the user's active
> membership in the caller's org and stores that membership.

Add to `SystemUser` in @public_api/models.py:

```python
scoped_to_membership = OrganizationForeignKey(
    "organizations.OrganizationMembership",
    related_name="scoped_system_users",
    on_delete=models.CASCADE,
    null=True,
    blank=True,
    db_index=True,
    help_text=(
        "When set, this token may only read/write data belonging to calendars owned by "
        "this organization membership's user. NULL = organization-wide token (legacy default)."
    ),
)
```

- `OrganizationForeignKey` (from `organizations.models`) — enforces `organization_id` in the JOIN ON
  clause; `SystemUser.organization` supplies the tenant side, the membership the target side. Nullable,
  indexed. `on_delete=CASCADE`: if the membership is deleted, its scoped tokens go with it.
- Migration is an additive nullable column on a low-volume table — no lock concern, no backfill.
  Reverse path = drop column.

### 3.2 Owner-derivation helper

New helper (module-level in @public_api/scoping.py) returning the owner's calendar-id set for a
request's system user. The owner's calendars are still keyed by `User` (`CalendarOwnership.user`), so
the helper joins from the membership to its user in a single query:

```python
def scoped_calendar_ids(system_user, organization) -> set[int] | None:
    """None => unrestricted (org-wide token). A set (possibly empty) => the
    only calendar ids this token may touch."""
    if system_user.scoped_to_membership_id is None:
        return None
    return set(
        Calendar.objects.filter_by_organization(organization.id)
        .filter(ownerships__user__organization_memberships=system_user.scoped_to_membership_id)
        .distinct()
        .values_list("id", flat=True)
    )
```

`None` is the unrestricted sentinel so the null-owner path stays a no-op. An empty set means "scoped,
but owns nothing" → reads empty, writes not-found. The `ownerships__user__organization_memberships`
join reaches the calendars owned by the membership's user without loading the membership row.

### 3.3 Type plumbing

- `PublicApiHttpRequest` already carries `public_api_system_user` + `public_api_organization` (set in @public_api/middlewares.py) — no change.
- Provider mint allow-list as a module constant in @public_api/constants.py (e.g. `PROVIDER_SCOPED_RESOURCES: frozenset[str]`).

## 4. API Design

### 4.1 `createScopedSystemUser` (GraphQL mutation)

- Location: @public_api/mutations.py, on the `Mutation` type.
- Permission: `[IsAuthenticated, OrganizationResourceAccess]`; add `createScopedSystemUser → SYSTEM_USER` to `FIELD_TO_RESOURCE_MAPPING` in @public_api/permissions.py.
- Input: `integration_name: str`, `scoped_to_user_id: int` (required for provider shape), `available_resources: list[str]`.
- Behavior: validate the owner resolves to a user in the caller's organization; validate `available_resources ⊆ PROVIDER_SCOPED_RESOURCES`; create the `SystemUser` with `scoped_to_user` set + the grants; return the plaintext token once. Duplicate `integration_name` → error.
- Response: `id`, `integration_name`, `is_active`, `available_resources`, `scoped_to_user_id`, write-once `token`.
- Errors: unknown/out-of-org owner → error; over-grant → error; duplicate name → error.

### 4.2 REST `POST /public-api-tokens/` — optional owner

- @public_api/serializers.py `SystemUserTokenCreateSerializer`: add optional `scoped_to_user` (int, `required=False`, `allow_null=True`). When present, validate it resolves to a user in the caller's org and enforce the provider allow-list; pass through to `create_system_user`. When absent, behavior is exactly as today.
- @public_api/services.py `create_system_user`: accept optional `scoped_to_user`.
- Response serializer gains `scoped_to_user` (read-only). Update/partial_update serializers unchanged → owner stays immutable.

### 4.3 Provider write mutations (new)

Three new mutations on the `Mutation` type, each `[IsAuthenticated, OrganizationResourceAccess]`-guarded and owner-checked. They wrap existing `CalendarService` methods (`create_available_time`, `create_blocked_time`, `create_event` — see [calendar_service.py:828-1361](../calendar_integration/services/calendar_service.py#L828-L1361)):

| Mutation | Resource | Wraps | Notes |
|---|---|---|---|
| `createAvailableTime` | `AVAILABLE_TIME` | `create_available_time` | rrule optional → covers recurring availability AND specific dates. |
| `createBlockedTime` | `BLOCKED_TIME` | `create_blocked_time` | rrule optional. |
| `scheduleEvent` | `CALENDAR_EVENT` | `create_event` | "schedule events" capability. |

Each takes a `calendar_id`; the resolver rejects (not-found) when a scoped token's `calendar_id ∉ scoped_calendar_ids(...)`.

## 5. Phased Rollout

### Phase 0 — Add `scoped_to_user` + owner-derivation helper

**Goal**: schema + shared helper in place. Ship value: none on its own — foundation consumed by every later phase.

**Feature flag**: none (additive nullable column; no reachable behavior change).

Changes:
1. @public_api/models.py: add `SystemUser.scoped_to_user` (per **Data Model Changes**).
2. Migration: additive nullable FK + index.
3. @public_api/scoping.py (new): `scoped_calendar_ids(system_user, organization)` helper.
4. @public_api/constants.py: `PROVIDER_SCOPED_RESOURCES` frozenset.
5. @public_api/admin/system_user.py: surface `scoped_to_user` read-only in admin.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @public_api/tests/test_scoping.py — helper returns `None` for null owner; returns only the owner's calendar ids; returns empty set for an owner with no calendars; respects organization filter (never returns another org's calendar).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single migration + small helper, but the helper has the org/ownership join worth getting right.

**Reusable skills**: `add-migration` (additive nullable FK).

Acceptance: migration applies and reverses cleanly; `scoped_calendar_ids` returns `None` for an org-wide token and the correct id set for a scoped one; full suite green with no behavior change to existing tokens.

### Phase 1 — Enforce owner scope on read queries

**Goal**: a scoped token's reads return only its owner's data; org-wide tokens unchanged.

**Feature flag**: none — gated by `scoped_to_user IS NULL` (helper returns `None` → no-op).

Changes:
1. @public_api/queries.py: in `calendars`, `calendar_events`, `blocked_times`, `available_times`, `availability_windows`, `unavailable_windows`, intersect the resolved queryset / the `calendar_id` lookup against `scoped_calendar_ids(...)`. For list queries with no `calendarId`, constrain to the owner's calendars instead of returning the org-wide set. For single-id lookups outside the set, return empty.
2. `_prepare_service_and_calendar` ([queries.py:90-100](../public_api/queries.py#L90-L100)): when the token is scoped and `calendar_id ∉` the owner set, raise the same not-found path used for a missing calendar (no existence leak).

Spec use-case: Use-cases entry "Provider token manages its owner's availability and schedule" (read portion) + "Provider token attempts to reach another provider's data" (read portion).

Tests:
- **Integration**: @public_api/tests/test_queries.py — scoped token lists only owner's calendars/events/blocked/available/windows; querying another provider's `calendarId` returns empty (not error that confirms existence); **org-wide token returns identical results to pre-change (flag-off-equivalent assertion)**.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches six resolvers with a shared invariant + the non-leak edge.

**Reusable skills**: `write-tests`.

Acceptance: every in-scope read query returns only the owner's data for a scoped token and unchanged results for an org-wide token.

### Phase 2 — `createScopedSystemUser` mutation

**Goal**: the bot can mint a provider-scoped token via GraphQL.

**Feature flag**: none — new mutation, additive surface.

Changes:
1. @public_api/mutations.py: add `createScopedSystemUser` (input, allow-list validation, owner-in-org validation, create + grants, write-once token) per **API Design**.
2. @public_api/permissions.py: map `createScopedSystemUser → SYSTEM_USER` in `FIELD_TO_RESOURCE_MAPPING`.
3. @public_api/services.py: extend `create_system_user` to accept `scoped_to_user`.

Spec use-case: Use-cases entry "Bot mints a provider-scoped token on provider creation".

Tests:
- **Integration**: @public_api/tests/test_mutations.py — caller with `SYSTEM_USER` grant mints a scoped token (token returned once, owner + grants persisted); caller lacking `SYSTEM_USER` is refused; owner outside caller's org rejected; over-grant (resource ∉ allow-list) rejected; duplicate `integration_name` rejected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Auth + validation + secret-once handling.

**Reusable skills**: `graphql-public-query` (mutation wiring + permission mapping); `write-tests`.

Acceptance: an authorized bot token mints a scoped provider token; all four rejection paths return errors; the plaintext token is revealed exactly once.

### Phase 3 — REST create accepts optional owner

**Goal**: org admins can mint a scoped token via the existing REST endpoint; the unscoped path is unchanged.

**Feature flag**: none — additive optional field.

Changes:
1. @public_api/serializers.py: `SystemUserTokenCreateSerializer` gains optional `scoped_to_user` with owner-in-org + allow-list validation; response serializer exposes `scoped_to_user` read-only.
2. @public_api/views.py: no signature change (serializer-driven); confirm `update`/`partial_update` never accept owner (immutability).
3. Regenerate OpenAPI schema (`schema.yml`).

Spec use-case: Use-cases entry "Admin mints a scoped token via REST".

Tests:
- **Integration**: @public_api/tests/test_views.py — admin creates a scoped token (owner persisted); **create with no owner is byte-for-byte identical to today**; over-grant rejected; owner outside org rejected; PUT/PATCH cannot change the owner.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Serializer field + validation mirroring an existing pattern.

**Reusable skills**: `create-rest-endpoint` (serializer/schema conventions); `write-tests`.

Acceptance: REST create makes a scoped token when an owner is supplied and an unchanged org-wide token when it isn't; owner is never mutable via update.

### Phase 4a — `createAvailableTime` mutation (owner-guarded)

**Goal**: a provider token can set recurring availability and specific availability dates.

**Feature flag**: none — new mutation; owner guard gated by scope.

Changes:
1. @public_api/mutations.py: `createAvailableTime(calendar_id, start, end, timezone, rrule?)` wrapping `CalendarService.create_available_time`; reject (not-found) when scoped and `calendar_id ∉` owner set.
2. @public_api/permissions.py: map `createAvailableTime → AVAILABLE_TIME`.

Spec use-case: Use-cases entry "Provider token manages its owner's availability and schedule" (recurring + specific availability write).

Tests:
- **Integration**: @public_api/tests/test_mutations.py — scoped token creates recurring + specific available time on its own calendar; creating on another provider's `calendar_id` returns not-found; org-wide token behavior unaffected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Mutation + owner guard + service wiring.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: scoped token writes available time only on owned calendars; cross-owner write is not-found.

### Phase 4b — `createBlockedTime` mutation (owner-guarded)

**Goal**: a provider token can add blocked times.

**Feature flag**: none.

Changes:
1. @public_api/mutations.py: `createBlockedTime(calendar_id, start, end, timezone, reason?, rrule?)` wrapping `CalendarService.create_blocked_time`; same owner guard.
2. @public_api/permissions.py: map `createBlockedTime → BLOCKED_TIME`.

Spec use-case: Use-cases entry "Provider token manages its owner's availability and schedule" (blocked-time write).

Tests:
- **Integration**: @public_api/tests/test_mutations.py — scoped token blocks time on own calendar; cross-owner `calendar_id` not-found; org-wide unaffected.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Mirrors Phase 4a precedent.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: scoped token blocks time only on owned calendars; cross-owner write is not-found.

### Phase 4c — `scheduleEvent` mutation (owner-guarded)

**Goal**: a provider token can schedule events on its owner's calendar.

**Feature flag**: none.

Changes:
1. @public_api/mutations.py: `scheduleEvent(calendar_id, event_data...)` wrapping `CalendarService.create_event`; same owner guard. Reuse existing event-input shapes from @calendar_integration/mutations.py where possible.
2. @public_api/permissions.py: map `scheduleEvent → CALENDAR_EVENT`.

Spec use-case: Use-cases entry "Provider token manages its owner's availability and schedule" (schedule-event write).

Tests:
- **Integration**: @public_api/tests/test_mutations.py — scoped token schedules an event on its own calendar; cross-owner `calendar_id` not-found; org-wide unaffected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Event input shape is richer than blocked/available.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: scoped token schedules events only on owned calendars; cross-owner write is not-found.

### Phase 5 — Cross-owner adversarial sweep + security review

**Goal**: prove the scope cannot be bypassed across the whole reachable surface, including nested fields.

**Feature flag**: none — tests + hardening only.

Changes:
1. @public_api/tests/test_scoping_security.py (new): for every field a scoped token can reach (queries from Phase 1, mutations from Phases 4a–c), assert no cross-owner read leak and no cross-owner write. Include **nested/expanded reads** — e.g. an event returned to a scoped token must not expose related objects belonging to another owner via field traversal.
2. Fix any leak the sweep surfaces (the only non-test changes in this phase).
3. Produce the bypass-surface checklist (enumerate every reachable field + its owner-guard) for security sign-off; record it alongside the spec.

Spec use-case: Use-cases entry "Provider token attempts to reach another provider's data" (full read + write coverage) — the consolidated negative-path guarantee.

Tests:
- **Integration**: the new sweep file above; plus a regression assertion that an org-wide token is unaffected by every guard.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` (`[1m]`) / `gpt-5` extended thinking / `gemini-3-pro`. Adversarial enumeration + nested-field reasoning is exactly where the bypass risk hides.

**Reusable skills**: `write-tests`.

Acceptance: the adversarial sweep passes (no read leak, no cross-owner write, including nested fields); the bypass-surface checklist is complete and signed off.

## 6. Risk & Rollout Notes

- **No feature flag.** Rollout safety comes from data gating: enforcement is a no-op while `scoped_to_user IS NULL`. Every enforcement phase (1, 4a–c) ships a test asserting org-wide tokens are unchanged. There is therefore no flag-removal phase.
- **Migration safety.** Phase 0 is an additive nullable indexed FK on the low-volume `SystemUser` table — no table rewrite, no hot-path lock, no backfill. Reverse = drop column; safe to roll back as long as no scoped token has been minted yet.
- **One-way-door caveat.** Once scoped tokens exist in production, rolling back the column would strand them (their auth still works but scope is lost → they'd silently widen to org-wide). Treat "first scoped token minted in prod" as the point of no easy return; verify Phases 1 + 4a–c + 5 are all deployed before the bot mints its first scoped token. Sequence: deploy enforcement (Phase 1) and write mutations (4a–c) and the security sweep (5) **before** enabling the bot to call Phase 2's mint mutation in production.
- **Leak surface.** The dominant risk is a read resolver or nested field that forgets to intersect with the owner set. Mitigated by two-layer enforcement, empty-on-read / not-found-on-write semantics, and the Phase 5 adversarial sweep over every reachable field.
- **Mint as escalation vector.** The mint mutation is guarded by `SYSTEM_USER` + owner-must-be-in-org; over-grant blocked by the fixed allow-list. Covered by Phase 2 rejection tests.
- **Deactivated owner.** No cascade needed — owner calendars re-derived per request. Documented behavior: a dead owner's scoped token reads empty / writes not-found.
- **Rollback story.** Each phase is independently reversible. Enforcement phases revert to org-wide behavior by reverting the resolver/mutation change (the column can stay). Mint phases revert by removing the mutation/serializer field. No data migration to unwind.

## 7. Open Questions

1. **Auto-revoke a scoped token when its owner is deactivated / leaves the org?** Recommended default: **no** — rely on per-request re-derivation (already denies access). Owner: platform security. If later required, add an owner-lifecycle signal in a follow-up; not in this plan.
2. **Patient token + single-use codes** (Spec non-goals). Deferred entirely. When the codes work is planned, a follow-up plan adds the patient mint shape (no owner, `{AVAILABILITY_WINDOWS, CALENDAR_EVENT}` allow-list) plus the code-gated read/write enforcement and auto-revoke-on-write. Owner: Medplum integration owner. Unblocks: the separate scheduling-code design landing.
3. **Exact provider allow-list membership** — default `{AVAILABLE_TIME, BLOCKED_TIME, CALENDAR_EVENT, AVAILABILITY_WINDOWS, UNAVAILABLE_WINDOWS, CALENDAR}`. Owner: Medplum integration owner. Confirm before Phase 2 ships; adjusting the frozenset is a one-line change.

## 8. Touch List

**Phase 0**
- Edit: [public_api/models.py](../public_api/models.py) (`SystemUser.scoped_to_user`)
- New: `@public_api/migrations/XXXX_systemuser_scoped_to_user.py`
- New: `@public_api/scoping.py`
- Edit: [public_api/constants.py](../public_api/constants.py) (`PROVIDER_SCOPED_RESOURCES`)
- Edit: [public_api/admin/system_user.py](../public_api/admin/system_user.py)
- New: `@public_api/tests/test_scoping.py`

**Phase 1**
- Edit: [public_api/queries.py](../public_api/queries.py) (six resolvers + `_prepare_service_and_calendar`)
- Edit: [public_api/tests/test_queries.py](../public_api/tests/test_queries.py)

**Phase 2**
- Edit: [public_api/mutations.py](../public_api/mutations.py) (`createScopedSystemUser`)
- Edit: [public_api/permissions.py](../public_api/permissions.py) (mapping)
- Edit: [public_api/services.py](../public_api/services.py) (`create_system_user` owner arg)
- Edit: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py)

**Phase 3**
- Edit: [public_api/serializers.py](../public_api/serializers.py) (optional owner + response field)
- Edit: [public_api/views.py](../public_api/views.py) (confirm immutability)
- Edit: `@schema.yml` (regenerate)
- Edit: [public_api/tests/test_views.py](../public_api/tests/test_views.py)

**Phase 4a / 4b / 4c**
- Edit: [public_api/mutations.py](../public_api/mutations.py) (`createAvailableTime` / `createBlockedTime` / `scheduleEvent`)
- Edit: [public_api/permissions.py](../public_api/permissions.py) (three mappings)
- Edit: [public_api/tests/test_mutations.py](../public_api/tests/test_mutations.py)

**Phase 5**
- New: `@public_api/tests/test_scoping_security.py`
- Edit: any resolver/mutation a leak is found in
- New: bypass-surface checklist doc alongside the spec in `ai-plans/`

## Amendments

- **2026-06-18** — Scope FK retargeted from `users.User` (plain `ForeignKey`) to
  `organizations.OrganizationMembership` (`OrganizationForeignKey`), renamed `scoped_to_user` →
  `scoped_to_membership`. Rationale: a membership is the unique `(user, org)` pair, so the scope is
  org-bound at the schema level and the FK gains `organization_id` in its JOIN ON clause (tenant
  safety); `users.User` has no `organization_id` and so cannot be an `OrganizationForeignKey` target.
  The external API contract is unchanged — callers still pass/receive `scoped_to_user_id` (a `User`
  id); mint resolves the user's active membership in the caller's org and stores the membership.
  Affected phases: 0 (model + helper), 2 + 3 (mint resolves membership), 4c (service ownership check
  hops membership→user), 1 + 4a + 4b + 5 (test factories). Branches force-pushed: phase-0, phase-1,
  phase-2, phase-3, phase-4a, phase-4b, phase-4c, phase-5.

- **2026-06-18** — Phase 0 merged to `main` (PR #104). Rebased the stack onto the new `main`, which had
  since absorbed the **public-graphql-service-wrappers** feature. Resolved conflicts and landed
  **phases 1–3** (read scoping, `createScopedSystemUser` mint, REST scoped create): reconstructed the
  `test_queries.py` / `test_mutations.py` append conflicts, and fixed a real integration regression —
  `main` made `SystemUser` an `OrganizationModel`, so the scoped tests' `SystemUser.objects.get/count(...)`
  now raise `ImproperlyConfigured`; switched those unscoped reads to `SystemUser.original_manager`
  (matching `main`'s own convention). Validated: phase-1 `test_queries` 111 passed, phase-2
  `test_mutations` 141 passed, phase-3 full `public_api` suite 503 passed. Branches force-pushed:
  phase-1, phase-2, phase-3.
- **2026-06-18 — Phases 4–5 PAUSED pending re-plan.** `main`'s public-graphql-service-wrappers feature
  already shipped a suite of **org-wide** public-API write mutations, including a `createBlockedTime`
  that **collides** (same GraphQL field name, different permission/scope/args/return) with phase-4b's
  owner-scoped `createBlockedTime`. Phase-4a (`createAvailableTime`) and phase-4c (`scheduleEvent`)
  have no `main` counterpart; phase-4a also conflicts with `main`'s relocation of the query helpers.
  Decision (user): land 1–3 now, **re-spec phases 4–5** (owner-scoped writes) against the new `main`
  rather than reconcile in place. The original phase-4a/4b/4c/5 branches (PRs #108–#111) are left
  intact at their pre-rebase tips for audit; they are NOT mergeable as-is and await the re-plan.
