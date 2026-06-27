# Bookable Slots for Single Calendars & Bundles, with Booking Policies — Implementation Plan

Spec: [2026-06-26-BOOKABLE_SLOTS_SINGLE_CALENDAR_AND_BOOKING_POLICY_SPEC.md](./2026-06-26-BOOKABLE_SLOTS_SINGLE_CALENDAR_AND_BOOKING_POLICY_SPEC.md). This plan translates that spec into phased delivery; it does not re-derive requirements. Where a phase implements a spec use-case, the **Decisions → Use-cases** id is named in the phase body.

## 1. Goals

1. **Single-calendar & bundle slot discovery.** A new `calendar_bookable_slots` GraphQL query (plus a `_with_code` variant) returns discretized, policy-compliant slots for any single calendar id, auto-expanding bundle calendars.
2. **A `BookingPolicy` model with a deterministic resolution chain.** Lead time, max horizon, buffer-before, and buffer-after attach to a calendar, owning membership, calendar group, or the organization default, and resolve in a fixed precedence order.
3. **Policies honored on every slot surface and at booking time.** The new single/bundle query, the `_with_code` variant, the existing group query, and the booking write path all apply the resolved policy — while remaining byte-for-byte identical when no policy exists anywhere.
4. **Full CRUD for policies on both APIs.** Create / read / update / delete on the private REST surface and the public GraphQL surface, organization-scoped and audited.

**Non-goals:**

- No new booking creation mechanics — enforcement hooks the *existing* write path; no reservation / hold / lock flow.
- No absolute calendar-date horizon cutoff — horizon is rolling-from-now only.
- No recurring, time-windowed, per-service, or per-appointment-type policies — a policy is a flat set of four values attached to calendar / membership / group / org.
- No REST surface for slot *discovery* — slot reads stay GraphQL-only; only policy CRUD gets a REST surface.
- No change to `availability_windows` / `unavailable_windows` (and their `_with_code` variants) — policies apply to *slot* surfaces and the booking path only.
- No feature-flag subsystem — see **Guiding Decisions**; the gate is data-presence.
- No client-side migration tooling and no timezone redesign — the new query is additive; existing IANA timezone handling is reused as-is.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Rollout gate (no feature flag)** | This codebase has **no feature-flag system**, and building one is disproportionate here. The two non-additive changes — the existing group query becoming policy-aware (Phase 7) and the booking write path gaining enforcement (Phases 8a/8b) — are **naturally inert when no `BookingPolicy` exists for the resolved scope**. "No policy anywhere ⇒ byte-for-byte identical output / no enforcement" is the backward-compat guarantee (spec **Acceptance scenarios** #5), enforced by a regression test in every existing-surface phase. Rollout = control which orgs get policies created. **This is the explicit justification for shipping these phases without a flag.** |
| **Policy storage shape** | One `BookingPolicy` row with **nullable target FKs** (`calendar`, `membership`, `calendar_group`) plus an `is_organization_default` boolean, and a **DB check constraint enforcing exactly one target**. Chosen over a generic `target_type`/`target_id` (loses FK integrity, fights the project's composite `OrganizationForeignKey`) and over per-target tables (4× the CRUD surface). Per-target partial unique indexes keep resolution unambiguous. |
| **Rule field encoding** | Four `PositiveIntegerField` second-counts: `lead_time_seconds`, `max_horizon_seconds`, `buffer_before_seconds`, `buffer_after_seconds`. **Zero = "no constraint for that field"** (lead 0 = bookable now, horizon 0 = unbounded, buffer 0 = flush allowed), matching spec **State transitions & edge cases**. `PositiveIntegerField` rejects negatives at the serializer/DB layer (spec **Acceptance scenarios** #6). |
| **Resolution precedence (single calendar)** | Calendar policy → owning-membership policy → org-default policy → no-constraints. Owning membership is resolved through `CalendarOwnership`: the lone owner when exactly one exists, else the `is_default=True` ownership when several exist, else **skip** the membership layer (resource/shared calendars). |
| **Resolution precedence (bundle/group)** | Explicit bundle/group policy overrides everything for that computation. Absent one, combine the **most-restrictive** field across **all** participants: `max(lead)`, `min(horizon>0)`, `max(buffer_before)`, `max(buffer_after)`. "Never offer a slot a participant would reject." |
| **Org default cardinality** | Exactly **one** optional org-default policy (a partial unique index on `is_organization_default=True` per org). A second create is rejected. |
| **Bundle bookability predicate** | A bundle slot is offered only when **every** `bundle_children` calendar is free for the window; `is_primary` gets no availability special-casing. |
| **New slot query shape** | A new `calendar_bookable_slots(calendar_id, …)` field auto-detects personal vs bundle; the existing `calendar_group_bookable_slots` stays its own field and *separately* gains policy-awareness. Smallest blast radius; each surface independently testable. |
| **`_with_code` response** | Returns **only** the filtered slot list — no policy rule values disclosed to anonymous bookers. |
| **Delete-absent semantics** | Deleting a policy for a target with none is an **idempotent no-op success** on both APIs. |
| **Audit** | `BookingPolicy` create/update/delete are business writes → emitted through `AuditService` (with diffs on update), consistent with the existing audit-trail rollout. Slot reads stay un-audited. |
| **Buffer needs blocking spans for managed calendars too** | The existing group walker checks managed calendars only against `AvailableTime` coverage (no event subtraction). The buffer rule is defined against existing `CalendarEvent`/`BlockedTime`, so the slot engine must fetch blocking spans for **all** target calendars (managed included) **when a buffer policy is in effect**. When no buffer applies, the managed path is unchanged — preserving byte-for-byte output. |

## 3. Data Model Changes

### 3.1 New `BookingPolicy` (in @calendar_integration/models.py)

```python
class BookingPolicy(OrganizationModel):
    """A flat set of booking guardrails attached to exactly one target:
    a calendar, an owning membership, a calendar group, or the org default.
    Zero on any field means 'no constraint for that field'."""

    calendar = OrganizationForeignKey(
        Calendar, on_delete=models.CASCADE, null=True, blank=True,
        related_name="booking_policies",
    )
    membership = OrganizationMembershipForeignKey(
        on_delete=models.CASCADE, null=True, blank=True,
        related_name="booking_policies",
    )
    calendar_group = OrganizationForeignKey(
        CalendarGroup, on_delete=models.CASCADE, null=True, blank=True,
        related_name="booking_policies",
    )
    is_organization_default = models.BooleanField(default=False)

    lead_time_seconds = models.PositiveIntegerField(default=0)
    max_horizon_seconds = models.PositiveIntegerField(default=0)   # 0 = unbounded
    buffer_before_seconds = models.PositiveIntegerField(default=0)
    buffer_after_seconds = models.PositiveIntegerField(default=0)

    objects = BookingPolicyManager()

    class Meta:
        constraints = [
            # exactly one target set (concrete columns of the composite FKs)
            models.CheckConstraint(
                name="bookingpolicy_exactly_one_target",
                check=(
                    <exactly-one-of: calendar_fk_id, membership_user_id,
                     calendar_group_fk_id, is_organization_default=True>
                ),
            ),
            models.UniqueConstraint(
                fields=["organization", "calendar_fk"],
                condition=models.Q(calendar_fk__isnull=False),
                name="bookingpolicy_uniq_calendar",
            ),
            models.UniqueConstraint(
                fields=["organization", "membership_user_id"],
                condition=models.Q(membership_user_id__isnull=False),
                name="bookingpolicy_uniq_membership",
            ),
            models.UniqueConstraint(
                fields=["organization", "calendar_group_fk"],
                condition=models.Q(calendar_group_fk__isnull=False),
                name="bookingpolicy_uniq_group",
            ),
            models.UniqueConstraint(
                fields=["organization"],
                condition=models.Q(is_organization_default=True),
                name="bookingpolicy_uniq_org_default",
            ),
        ]
```

Notes:
- The check constraint references the **concrete** columns the composite `OrganizationForeignKey`/`OrganizationMembershipForeignKey` contribute (`calendar_fk_id`, `membership_user_id`, `calendar_group_fk_id`) — exact SQL is the `migration-author`'s concern.
- Export `BookingPolicy` from any model `__init__`/`__all__` aggregation the app uses; register a `BookingPolicyFactory` and admin.
- `on_delete=CASCADE` to a target: deleting the calendar/group/membership removes its policy; resolution then falls through. Matches spec's "delete ⇒ resolver falls to next layer."

### 3.2 `BookingPolicyManager` / queryset

Minimal custom manager (`filter_by_organization` is inherited via `OrganizationModel`). Add a `for_target(...)` convenience and an `org_default()` lookup used by the resolver.

### 3.3 Type plumbing — `EffectivePolicy` (in @calendar_integration/services/dataclasses.py)

```python
@dataclass(frozen=True)
class EffectivePolicy:
    lead_time: datetime.timedelta
    max_horizon: datetime.timedelta | None   # None = unbounded
    buffer_before: datetime.timedelta
    buffer_after: datetime.timedelta

    @classmethod
    def unconstrained(cls) -> "EffectivePolicy": ...
    @classmethod
    def from_model(cls, policy: "BookingPolicy") -> "EffectivePolicy": ...   # 0 horizon -> None
    @staticmethod
    def most_restrictive(policies: Iterable["EffectivePolicy"]) -> "EffectivePolicy": ...
```

The resolver returns `EffectivePolicy`; the slot engine and booking path consume it. Slot results carry the existing `BookableSlotProposal` dataclass unchanged.

## 4. API Design

### 4.1 New GraphQL query — single/bundle slot discovery (Phases 5 & 6)

Mirror the existing `calendar_group_bookable_slots` signature, swapping `group_id` for `calendar_id`:

- `calendar_bookable_slots(calendar_id, search_window_start, search_window_end, duration_seconds, slot_step_seconds=900) -> list[BookableSlotProposalGraphQLType]` — authenticated; permission classes `[IsAuthenticated, OrganizationResourceAccess]`.
- `calendar_bookable_slots_with_code(code, search_window_start, search_window_end, duration_seconds, slot_step_seconds=900) -> list[BookableSlotProposalGraphQLType]` — code-gated; reuses the `MAX_CODE_GATED_RANGE` clamp and the uniform "Invalid or expired code." error already used by `*_with_code` fields in @public_api/queries.py.

Reuses `BookableSlotProposalGraphQLType` from @calendar_integration/graphql.py. Register field→resource in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` (@public_api/permissions.py) under a new `PublicAPIResources.BOOKABLE_SLOTS` (@public_api/constants.py).

### 4.2 Policy CRUD — private REST (Phase 3)

`BookingPolicyViewSet(VintaScheduleModelViewSet)` registered in @calendar_integration/routes.py (regex `booking-policies`). Org-scoping is automatic via `TenantScopedViewMixin`; `get_queryset` filters by `request.organization`. `BookingPolicySerializer` validates: exactly-one-target, non-negative fields (via `PositiveIntegerField`), and create-uniqueness (clear 400 on duplicate target). `destroy` overridden for idempotent no-op when the target has no policy. `schema.yml` regenerated.

### 4.3 Policy CRUD — public GraphQL (Phase 4)

- Query: `booking_policies(...)` → `list[BookingPolicyGraphQLType]` (filterable by target).
- Mutations: `create_booking_policy`, `update_booking_policy`, `delete_booking_policy` (idempotent no-op delete).
- `BookingPolicyGraphQLType` + input types in @calendar_integration/graphql.py. Permission classes `[IsAuthenticated, OrganizationResourceAccess]`; field→resource mapping under a new `PublicAPIResources.BOOKING_POLICY`.

Both surfaces delegate writes to the same `BookingPolicyService` create/update/delete methods so validation, uniqueness, and audit live in one place.

## 5. Phased Rollout

Phase granularity follows the Step 0 choice to **bundle closely-related use-cases** while keeping every phase MR-sized (≤1500 LoC incl. tests), one concern, and independently mergeable. No feature flag is declared (see **Guiding Decisions**), so there is **no flag-removal phase**; instead, every existing-surface phase ships a "no policy ⇒ unchanged" regression test as the equivalent guard.

Ordering puts the keystone dependency (model → resolver) first, then the additive surfaces, then the two existing-surface changes that carry backward-compat risk, then enforcement last (so a policy can already be created before any write is rejected).

### Phase 1 — Scaffold the `BookingPolicy` model

**Goal**: persist `BookingPolicy` rows with the full constraint set. Ship value: none on its own — foundation the rest consumes.

**Feature flag**: none — purely additive new table; no existing code reads or writes it.

Changes:
1. @calendar_integration/models.py: add `BookingPolicy` (fields, check constraint, four partial unique indexes) + `BookingPolicyManager`. Export it where the app aggregates models.
2. Migration: create table + constraints. The check constraint and composite-FK concrete columns are raw-SQL-adjacent — authored via the `migration-author`.
3. Admin registration + `BookingPolicyFactory` for tests.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit** (@calendar_integration/tests/): factory builds valid rows; the check constraint rejects zero-target and multi-target rows; each partial unique index rejects a duplicate (calendar, membership, group, org-default); negative field values rejected.

**Suggested AI model**: Tier 1 for the model/admin/factory; step up to **Tier 4** for the migration (check constraint + partial uniques against composite-FK concrete columns on a multi-tenant table). IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `Skill(add-model)`; `Skill(add-migration)` (couples with the `migration-author` sub-agent).

Acceptance: migration applies and reverses cleanly; a `BookingPolicy` can be created against exactly one target and only one; duplicate-target and multi-target inserts raise `IntegrityError`.

### Phase 2 — Effective-policy resolver service

**Goal**: given a calendar, bundle, or group, return the resolved `EffectivePolicy` via the full precedence chain. Ship value: none user-visible — the engine the slot/booking phases call.

**Feature flag**: none — new service, no caller in any existing path yet.

Changes:
1. @calendar_integration/services/dataclasses.py: add `EffectivePolicy` (`unconstrained`, `from_model`, `most_restrictive`).
2. New @calendar_integration/services/booking_policy_service.py — `BookingPolicyService` with:
   - `resolve_for_calendar(calendar) -> EffectivePolicy` (calendar → owning-membership via `CalendarOwnership` lone/`is_default` rule → org default → unconstrained).
   - `resolve_for_bundle(bundle_calendar)` / `resolve_for_group(group)` (explicit override → `most_restrictive` across participants → unconstrained).
   - Create/update/delete methods (used by Phases 3 & 4) that enforce exactly-one-target + create-uniqueness and emit `AuditService` records.
3. @di_core/containers.py: add `booking_policy_service = providers.Factory(BookingPolicyService, …)` alongside the existing calendar services (inject `audit_service`).

Spec use-case: implements the **State transitions & edge cases → Effective-policy resolution** chart; consumed by use-cases 1–5.

Tests:
- **Unit**: the full precedence matrix — calendar-only, membership-only, org-default-only, each absent, and fallthrough to unconstrained; owning-membership ambiguity (zero owners → skip; one owner → use; multiple owners → `is_default`); `most_restrictive` field-by-field (`max` lead/buffers, `min` positive horizon); explicit bundle/group override beats per-participant combination.

**Suggested AI model**: **Tier 3** — multi-branch resolution logic across `CalendarOwnership`, bundle children, and group participants with the most-restrictive combination. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: none — pure service + DI wiring.

Acceptance: `BookingPolicyService.resolve_for_*` returns the spec-correct `EffectivePolicy` for every branch of the resolution matrix; unconstrained when nothing applies.

### Phase 3 — Policy CRUD on the private REST API

**Goal**: org admins create/read/update/delete policies over REST. Implements spec use-case 3 (REST half).

**Feature flag**: none — brand-new endpoint at a new path; no existing REST surface changes.

Changes:
1. @calendar_integration/serializers.py: `BookingPolicySerializer` — exactly-one-target validation, non-negative fields, create-uniqueness 400.
2. @calendar_integration/views.py: `BookingPolicyViewSet(VintaScheduleModelViewSet)` — org-scoped `get_queryset`; `create`/`update`/`destroy` delegate to `BookingPolicyService`; `destroy` is an idempotent no-op when absent.
3. @calendar_integration/routes.py: register `booking-policies`.
4. Regenerate `schema.yml`.

Spec use-case: **Use-cases #3** (private REST).

Tests:
- **Integration**: authorized CRUD happy paths; unauthorized/forbidden (non-member org, wrong org header) paths; duplicate-target create → 400 naming the conflict; negative buffer → 400 naming the field (**Acceptance scenarios** #6); delete-absent → 2xx no-op; audit record emitted on create/update/delete.

**Suggested AI model**: **Tier 2** — DRF serializer with non-trivial validation + ViewSet wiring against the established `VintaScheduleModelViewSet` base. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `Skill(create-rest-endpoint)`.

Acceptance: REST CRUD passes with organization-scope enforcement; duplicate/invalid rejected; delete-absent succeeds; writes are audited.

### Phase 4 — Policy CRUD on the public GraphQL API

**Goal**: external integration partners create/read/update/delete policies over GraphQL. Implements spec use-case 3 (GraphQL half).

**Feature flag**: none — brand-new query + mutations; no existing GraphQL field changes.

Changes:
1. @calendar_integration/graphql.py: `BookingPolicyGraphQLType` + create/update input types.
2. @public_api/queries.py: `booking_policies(...)` query.
3. @public_api/mutations.py: `create_booking_policy`, `update_booking_policy`, `delete_booking_policy` — delegate to `BookingPolicyService` (shared validation + audit).
4. @public_api/constants.py: add `PublicAPIResources.BOOKING_POLICY`; @public_api/permissions.py: map the four fields in `FIELD_TO_RESOURCE_MAPPING`.

Spec use-case: **Use-cases #3** (public GraphQL).

Tests:
- **Integration**: authorized CRUD; missing-resource → permission error; cross-org isolation; duplicate-target and invalid-field rejections; idempotent delete; audit on writes.

**Suggested AI model**: **Tier 3** — strawberry-django type + three mutations + query, permission/resource wiring, sharing the service layer with Phase 3. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `Skill(create-graphql-public-query)`.

Acceptance: GraphQL CRUD passes with organization-scope + resource enforcement; behavior matches the REST surface; writes are audited.

### Phase 5 — `calendar_bookable_slots` query: single calendar + bundle

**Goal**: a consumer fetches policy-compliant slots for one calendar id, with bundles auto-expanded. Implements spec use-cases 1 **and** 2 (one new surface, closely related).

**Feature flag**: none — additive new GraphQL field; existing fields untouched. (The *behavior* it depends on is policy resolution, itself inert when no policy exists.)

Changes:
1. Extract the existing slot walker's reusable parts (`_fetch_available_spans`, `_fetch_blocking_spans`, `_split_calendars_by_management`, the candidate cursor loop) from @calendar_integration/services/calendar_group_service.py into a shared `slot_engine` module (module-level functions), leaving the group method delegating to them (pure refactor, covered by existing group tests). See `find_bookable_slots` at [calendar_group_service.py:963-1055](../calendar_integration/services/calendar_group_service.py#L963-L1055).
2. Add a **policy filter** to the engine: given candidates, an `EffectivePolicy`, and request `now`, drop candidates with `start < now + lead_time`, `start > now + max_horizon`, or whose `[start - buffer_before, end + buffer_after]` envelope overlaps any blocking span. **When a buffer applies, fetch blocking spans for managed calendars too** (see **Guiding Decisions**); when the policy is unconstrained, skip all of this so output is unchanged.
3. New `find_bookable_slots_for_calendar(calendar_id, …)` on a service (e.g. `BookableSlotsService` or `CalendarService`) that: detects bundle vs personal; for bundles requires **all** `bundle_children` free (via `ChildrenCalendarRelationship`); resolves the `EffectivePolicy` through `BookingPolicyService`; runs the engine + policy filter.
4. @public_api/queries.py: `calendar_bookable_slots(...)` field; resource mapping + `PublicAPIResources.BOOKABLE_SLOTS`.

Spec use-case: **Use-cases #1, #2**.

Tests:
- **Integration**: single managed + single unmanaged calendar return the same slots a one-calendar group would (**Objectives** #1 signal); bundle with two free children yields the slot, a busy child suppresses it (**Acceptance scenarios** #2); lead-time, buffer-before/after, and horizon rules each drop the expected candidates (**Acceptance scenarios** #3, #4); empty window / step ≥ window / empty bundle → `[]`; **no-policy run is identical to the pre-policy engine output**.
- **Unit**: the policy filter and `most_restrictive` interplay at boundary instants (slot exactly at `now + lead`, envelope exactly touching a blocking span).

**Suggested AI model**: **Tier 4** — refactoring the shared slot engine plus per-candidate policy math (envelope overlap, managed-calendar buffer fetch) against the batched-fetch design, with a byte-for-byte backward-compat constraint. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: none — internal service/engine work; query registration follows the `create-graphql-public-query` pattern already used in Phase 4.

Acceptance: `calendar_bookable_slots` returns policy-compliant discretized slots for personal and bundle calendars; a busy bundle child suppresses a slot; with no policy, results match the pre-feature engine.

### Phase 6 — `calendar_bookable_slots_with_code` (code-gated)

**Goal**: an anonymous booker on a public link fetches compliant slots with a booking code. Implements spec use-case 4.

**Feature flag**: none — additive new code-gated field.

Changes:
1. @public_api/queries.py: `calendar_bookable_slots_with_code(code, …)` — validate/resolve the code via the existing `resolve_code` path, derive the calendar scope, reuse the Phase 5 service, return **only** slots (no policy values). Reuse `MAX_CODE_GATED_RANGE` and the uniform invalid-code error.

Spec use-case: **Use-cases #4**.

Tests:
- **Integration**: valid code returns the same slots the authenticated query would for that calendar/bundle; invalid/expired/revoked code → uniform error; group-scoped code rejected (single/bundle only); response omits policy rule values; range beyond `MAX_CODE_GATED_RANGE` clamped/rejected as the existing `_with_code` fields do.

**Suggested AI model**: **Tier 2** — mirrors the existing `*_with_code` query plumbing, delegating to the Phase 5 service. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: `Skill(create-graphql-public-query)`.

Acceptance: the code-gated variant returns the same compliant slots as the authenticated query for the resolved scope, exposes only slots, and enforces the existing code-validation contract.

### Phase 7 — Make the existing group slot query policy-aware

**Goal**: `calendar_group_bookable_slots` (and its `_with_code` variant) apply the resolved group policy. Implements spec **Objectives** #3 for the group surface.

**Feature flag**: none — gated by **data-presence**. With no policy resolvable for the group, the engine path is the pre-feature path, byte-for-byte (the off-state this phase must prove).

Changes:
1. @calendar_integration/services/calendar_group_service.py: after building candidates, resolve the group's `EffectivePolicy` via `BookingPolicyService.resolve_for_group` and apply the Phase 5 policy filter. When unconstrained, skip entirely.
2. No GraphQL signature change — same fields, same response type.

Spec use-case: **Objectives #3** (existing group query) / **Acceptance scenarios** #5.

Tests:
- **Integration**: **regression — with no policy anywhere, output is byte-for-byte identical to the pre-feature group query** (snapshot/equality against the prior result); with a group override, slots are filtered; with only per-participant policies, the most-restrictive combination is applied.

**Suggested AI model**: **Tier 3** — wiring the resolver + filter into the existing group walk while guaranteeing the no-policy path is untouched. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: none.

Acceptance: the group query filters by the resolved group policy; with no policy anywhere its output is identical to before this phase.

### Phase 8a — Booking-time enforcement: single, bundle & code-gated single

**Goal**: a booking that violates the resolved policy on the single/bundle write path is rejected. Implements spec use-case 5 for `create_event`.

**Feature flag**: none — gated by **data-presence**. No resolvable policy ⇒ no check ⇒ pre-feature behavior (the off-state this phase must prove).

Changes:
1. Add `BookingPolicyViolationError` to the calendar_integration error types.
2. @calendar_integration/services/calendar_service.py / @calendar_integration/services/calendar_event_service.py: in the `create_event` path (single + bundle; also reached by `create_calendar_event_with_code`), resolve the `EffectivePolicy` for the target and check lead / horizon / buffer against current calendar state inside the existing booking transaction; raise on violation.
3. Map the new error to a useful GraphQL message in @public_api/mutations.py `schedule_event` and in @calendar_integration/mutations.py `create_calendar_event_with_code` (uniform error for the code path).

Spec use-case: **Use-cases #5** (single, bundle, code-gated single).

Tests:
- **Integration**: bookings inside lead / beyond horizon / inside buffer rejected on the authenticated and code-gated single-calendar paths and the bundle path; a compliant booking succeeds; **no-policy ⇒ unchanged write behavior**; a slot that became invalid between discovery and booking is rejected at write (the intended guard).

**Suggested AI model**: **Tier 3** — hooking enforcement into the transactional write path across single/bundle/code-gated callers with clean error mapping. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: none.

Acceptance: a policy-violating single or bundle booking (auth or code-gated) is rejected with an explanatory error and no event/blocked time is created; compliant bookings succeed; no-policy bookings behave as before.

### Phase 8b — Booking-time enforcement: group bookings

**Goal**: a group booking that violates the resolved group policy is rejected. Implements spec use-case 5 for the group write path.

**Feature flag**: none — gated by **data-presence** (same off-state guarantee).

Changes:
1. @calendar_integration/services/calendar_group_service.py: in the group booking method, resolve the group `EffectivePolicy` and apply the same lead/horizon/buffer check before persisting; raise `BookingPolicyViolationError` on violation.
2. Map the error at the group booking mutation surface.

Spec use-case: **Use-cases #5** (group).

Tests:
- **Integration**: group booking inside lead / beyond horizon / inside buffer rejected (**Acceptance scenarios** #7 covers the code-gated horizon case — assert it here for the group path and in 8a for single); compliant group booking succeeds; no-policy ⇒ unchanged.

**Suggested AI model**: **Tier 3** — group write path enforcement mirroring Phase 8a. IDs per tier in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml).

**Reusable skills**: none.

Acceptance: a policy-violating group booking is rejected with an explanatory error and nothing is persisted; compliant group bookings succeed; no-policy group bookings behave as before.

## 6. Risk & Rollout Notes

- **No feature flag — data-presence gate.** The gate is "does a `BookingPolicy` resolve for this scope." Every existing-surface phase (7, 8a, 8b) ships a regression test asserting that with **no policy anywhere** the output/behavior is unchanged (spec **Acceptance scenarios** #5). Production rollout = create policies for one internal org first, soak, then widen. Rollback = delete the org's policies (instantly reverts to pre-feature behavior) and/or revert the phase. Because there is no flag, there is **no flag-removal phase**.
- **Migration safety (Phase 1).** New table — no lock on hot tables. The check constraint + four partial unique indexes are created on an empty table, so no backfill and no rewrite. Authored via `migration-author`; reverse path drops table + constraints.
- **Changing the public group query's output (Phase 7).** Any org that sets a policy will see fewer/filtered group slots. Announce to integration partners before enabling policies (spec **Risks assumed** #1). The byte-for-byte regression test bounds the blast radius to policy-holding orgs only.
- **Per-candidate cost (Phase 5).** Buffer checks add envelope-overlap work per candidate, and managed calendars now fetch blocking spans when a buffer applies. Mitigation: reuse the already-batched span fetch; the work is skipped entirely when the policy is unconstrained. Revisit the SQL-generation alternative only if a hot tenant regresses.
- **Discovery/booking divergence.** Accepted: discovery is best-effort, booking is authoritative (enforced in-transaction at write time, Phases 8a/8b). No further mitigation.
- **No backfill required.** All four duration fields default to 0 (= no constraint); existing data is unaffected.

## 7. Open Questions

All five spec **Open questions** were resolved during Step 0:

1. **Owning-membership resolution** — resolved: lone owner, else `is_default` ownership when several, else skip to org default. (No residual question.)
2. **Bundle bookability** — resolved: all `bundle_children` must be free.
3. **Org-default attachment/cardinality** — resolved: one optional org-default policy per org (partial unique index).
4. **Delete-absent contract** — resolved: idempotent no-op success on both APIs.
5. **`_with_code` response shape** — resolved: slots only.

Residual (recommended default, owner = scheduling/calendar domain owner; resolve before the relevant phase, not blocking earlier phases):

- **Resource name granularity for the new slot query** — default: a single `PublicAPIResources.BOOKABLE_SLOTS` covering both `calendar_bookable_slots` and the code-gated variant; confirm during Phase 5 whether partners need it split from `CALENDAR`.
- **Horizon encoding readability** — default: `0 = unbounded` on `max_horizon_seconds` (spec-literal "zero = no constraint"). If the implementer finds `0`-as-unbounded error-prone, a nullable column is an acceptable equivalent; decide in Phase 1, keep the resolver's `EffectivePolicy.max_horizon: timedelta | None` contract either way.

## 8. Touch List

**Phase 1 — model**
- @calendar_integration/models.py (new `BookingPolicy` + `BookingPolicyManager`)
- @calendar_integration/admin.py (register)
- @calendar_integration/factories.py (new `BookingPolicyFactory`)
- New migration under @calendar_integration/migrations/ (table + check + partial uniques)

**Phase 2 — resolver**
- [dataclasses.py](../calendar_integration/services/dataclasses.py) (`EffectivePolicy`)
- @calendar_integration/services/booking_policy_service.py (new)
- [containers.py](../di_core/containers.py) (`booking_policy_service` provider)

**Phase 3 — REST CRUD**
- [serializers.py](../calendar_integration/serializers.py) (`BookingPolicySerializer`)
- [views.py](../calendar_integration/views.py) (`BookingPolicyViewSet`)
- [routes.py](../calendar_integration/routes.py) (register)
- [schema.yml](../schema.yml) (regenerate)

**Phase 4 — GraphQL CRUD**
- [graphql.py](../calendar_integration/graphql.py) (`BookingPolicyGraphQLType` + inputs)
- [queries.py](../public_api/queries.py) (`booking_policies`)
- [mutations.py](../public_api/mutations.py) (create/update/delete)
- [constants.py](../public_api/constants.py) (`BOOKING_POLICY`)
- [permissions.py](../public_api/permissions.py) (`FIELD_TO_RESOURCE_MAPPING`)

**Phase 5 — single/bundle slot query**
- New `slot_engine` module + refactor of [calendar_group_service.py](../calendar_integration/services/calendar_group_service.py#L963-L1055)
- New/extended service: `find_bookable_slots_for_calendar`
- [queries.py](../public_api/queries.py) (`calendar_bookable_slots`)
- [constants.py](../public_api/constants.py) (`BOOKABLE_SLOTS`) + [permissions.py](../public_api/permissions.py)

**Phase 6 — code-gated slot query**
- [queries.py](../public_api/queries.py) (`calendar_bookable_slots_with_code`)

**Phase 7 — group query policy-awareness**
- [calendar_group_service.py](../calendar_integration/services/calendar_group_service.py) (resolve + filter in the group walk)

**Phase 8a — enforcement: single/bundle/code-gated**
- @calendar_integration/errors (or equivalent) — `BookingPolicyViolationError`
- [calendar_service.py](../calendar_integration/services/calendar_service.py) / [calendar_event_service.py](../calendar_integration/services/calendar_event_service.py) (`create_event` check)
- [mutations.py](../public_api/mutations.py) (`schedule_event` error mapping) + @calendar_integration/mutations.py (`create_calendar_event_with_code` mapping)

**Phase 8b — enforcement: group**
- [calendar_group_service.py](../calendar_integration/services/calendar_group_service.py) (group booking check)
- group booking mutation surface (error mapping)
