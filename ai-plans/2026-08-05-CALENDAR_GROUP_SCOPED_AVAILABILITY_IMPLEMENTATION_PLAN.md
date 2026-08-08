# Calendar Group-Scoped Availability — Implementation Plan

Spec: [2026-08-05-CALENDAR_GROUP_SCOPED_AVAILABILITY_SPEC.md](2026-08-05-CALENDAR_GROUP_SCOPED_AVAILABILITY_SPEC.md). This plan translates that spec into phases; it does not re-derive requirements. Where a phase implements a spec use-case, the use-case is named under the phase's **Spec use-case** line, referring to entries under the spec's **Decisions → Use-cases**.

## 1. Goals

1. A calendar in a group slot can be narrowed by **group-scoped availability windows** (intersect-only), removed from time by **group-scoped blocked time**, and capped by **quota rules**, without any of it affecting the calendar's behavior in other groups or in single-calendar booking.
2. All three concepts are honored identically on four surfaces: group slot discovery, booking and rescheduling validation, the public API, and the internal REST management surface.
3. Groups with no group-scoped configuration produce output identical to today, with no additional queries on that path.
4. All writes are audited with before/after values, and permission-gated to the calendar's owner within groups they can see, with admin override.

**Non-goals:**

- Availability narrowed by event type or service rather than by group.
- Bulk copy of configuration across groups or calendars.
- Group throughput caps ("the group does at most 10 a week") — quota is per calendar only.
- Rolling quota windows — fixed calendar periods only.
- Admin override to book past a consumed quota.
- Automatic cancellation, rescheduling, or attendee notification for bookings orphaned by a narrowing.
- Any mechanism that makes a calendar bookable at a time its base availability excludes.
- Optimistic locking or conflict surfacing on concurrent edits — last-write-wins is accepted.
- Changes to single-calendar booking, to the existing availability read/write contract, or to booking-policy resolution.
- New metrics or dashboards — the audit trail is the only observability added.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Storage shape** | A nullable group-slot reference on the existing `AvailableTime` and `BlockedTime` tables, rather than new models. Each recurring model in this codebase carries four versioned raw-SQL functions (`calculate_recurring_*`, `get_*_occurrences_json`, and `_with_bulk_modifications` variants of each), an exception model, a bulk-modification model, and a queryset. New models would duplicate eight SQL artifacts to obtain behavior that already exists and is already tested. |
| **Row scoping** | The default manager on both models **excludes** group-scoped rows; group-scoped access is explicit opt-in. Chosen over per-call-site filtering because a missed call site is a silent correctness failure — a group-scoped window leaking into base availability makes someone bookable when they should not be. Default-exclude means every existing call site is correct with zero edits. |
| **Intersect, never widen** | Group-scoped windows narrow the calendar's base availability and can never extend it. Base availability stays the single source of truth for when a person works at all, which is what makes the fall-through default safe. |
| **Fall-through default** | No group-scoped configuration means base availability applies unchanged. This is what delivers goal 3 and is also the rollout gate (see below). |
| **Quota storage** | A small non-recurring model. Multiple rules per (calendar, slot), **all** of which must pass, so "at most 1 a day and 3 a week" is expressible. |
| **Quota counting** | Only bookings made **through that group** count. Events created directly on the calendar do not consume group quota — precise and cheap to compute, at the cost of a quota that reality can exceed. |
| **Quota evaluation** | A new versioned Postgres function returning per-calendar per-period booking counts, matching how occurrence expansion is already done in this codebase. Counts are derived on read, never stored, so cancellation frees quota immediately and a reschedule across a period boundary moves the count automatically. |
| **Week start** | A choice field on the organization model with a database default of Monday, matching the precedent of the other org policy fields. Used for quota period boundaries and nothing else. |
| **No feature flag — waiver** | This project has no feature-flag library; the billing rollout stated so explicitly and used its `unlimited` plan as the switch. Introducing one is out of scope. The waiver is sound here because the feature is **self-gating**: an unconfigured calendar takes the fall-through path and behaves byte-for-byte as today, so nothing changes for anyone until an admin deliberately creates a window. Every phase that touches an existing read path carries a test asserting the unconfigured path is unchanged, which is the same guarantee a flag-off test would give. The one phase that is **not** self-gating is the blocked-time metering change, which is called out separately below. |
| **Metering** | All blocked time becomes metered, base rows included — not only group-scoped blocks — so one rule holds: every time window an organization authors is metered, positive or negative. |
| **Metering rollout** | Switched on immediately. The product is **pre-customer**, so there is no installed base whose usage jumps and no organization that lands over its limit — the change that would be breaking later is free now. Measuring first and raising limits was considered and declined because there is nothing to measure. It still gets its own phase, for reviewability and independent revert, not because it is dangerous. |
| **Public API shape** | New dedicated operations for all three concepts. The existing availability query and batch mutation keep their exact current shape — the spec's negative scope freezes that contract, and partners depend on it. |
| **Internal REST shape** | Nested under the group slot, so listing a slot's roster is one call and group-visibility permissions apply at the route rather than per object. |
| **Write semantics** | Bulk upsert, replay is a no-op, last-write-wins on concurrent edits — identical to the existing availability batch write so integrations need not learn a second contract. |
| **Error detail** | Booking rejections name the calendar and the rule type violated, not the configured values. Enough for an admin to act; does not leak roster detail to external bookers on public links. |
| **Phase granularity** | Bundled by concept — foundation, then windows, then blocks, then quota — with sub-phases per surface where a concept exceeds MR size. Chosen over one-phase-per-use-case in Step 0. |
| **Assumption to verify in Phase 0** | The occurrence SQL functions take row ids as input, so scoping is applied by the caller before the function runs and the functions themselves need no change. Phase 0 verifies this rather than assuming it; if any function selects rows itself, it needs a version bump and Phase 0 grows accordingly. |

## 3. Data Model Changes

### 3.1 `AvailableTime.group_slot` and `BlockedTime.group_slot`

Nullable organization-scoped foreign key to `CalendarGroupSlot` on both models in [calendar_integration/models.py](calendar_integration/models.py), `on_delete=CASCADE` so the spec's "deleted with the membership" rule falls out of the schema rather than application code.

Null means a base row (today's behavior). Non-null means the row applies only when the calendar is evaluated inside that slot.

An index on the group-slot column supports the group-scoped read path. A partial index restricted to non-null rows keeps it small, since the overwhelming majority of rows are and will remain base rows.

### 3.2 Manager and queryset scoping

`AvailableTimeManager` and `BlockedTimeManager` in [calendar_integration/managers.py](calendar_integration/managers.py) filter to `group_slot IS NULL` by default. Explicit accessors expose the other views — group-scoped rows for one slot, and an unscoped escape hatch for admin and migrations.

The corresponding querysets in [calendar_integration/querysets.py](calendar_integration/querysets.py) gain the matching methods. `AvailableTimeQuerySet.only_user_authored` — which the billing counter relies on — must keep working across both scopes, since group-scoped windows are metered too.

### 3.3 New `CalendarGroupSlotQuotaRule`

A non-recurring organization-scoped model: the group slot, the calendar, the period (day / week / month), and the cap. Unique per (calendar, slot, period) so at most one rule per period per pair, while allowing the daily-plus-weekly combination.

Deleted with the slot membership, matching the windows.

### 3.4 `Organization.week_start`

A choice field on the organization model in [organizations/models.py](organizations/models.py), database default Monday, mirroring how `external_event_update_policy` is declared. Read only by quota period math.

### 3.5 New Postgres function — per-period booking counts

A versioned raw-SQL function under [calendar_integration/migrations/sql/functions/](calendar_integration/migrations/sql/functions/), authored through the framework at [common/raw_sql_migration_managers.py](common/raw_sql_migration_managers.py). Given a set of calendar ids, a group, a period type, a week start, and a search window, it returns per-calendar per-period counts of live bookings made through that group.

### 3.6 Type plumbing

The dataclasses in [calendar_integration/services/dataclasses.py](calendar_integration/services/dataclasses.py) that carry slot availability and bookable-slot proposals gain the fields needed to express a rejection reason (calendar plus rule type), and the write paths gain a result type carrying the orphaned-booking warning.

## 4. API Design

### 4.1 Internal REST — nested under the group slot

Collections for windows, blocks, and quota rules hang off a group's slot. Standard list / create / update / delete, organization-scoped, gated so a calendar owner may act on their own calendar within groups they can see, and an org admin on anyone's.

The narrowing write returns, alongside the saved object, the list of confirmed future bookings in that group that now fall outside the configuration. Nothing is cancelled.

### 4.2 Public API — new dedicated operations

New queries and batch-upsert mutations for the three concepts, using the same auth, organization scoping, and resource-mapping conventions as the existing availability operations, registered in [public_api/queries.py](public_api/queries.py) and [public_api/mutations.py](public_api/mutations.py).

Batch semantics match the existing availability batch write exactly: all-or-nothing, replay is a no-op, and a batch that would exceed the plan limit is rejected whole with the existing over-limit response body.

The existing availability query and batch mutation are **not** modified.

### 4.3 Errors

A booking or reschedule that names a calendar violating any of the three rules is rejected with the calendar identifier and the rule type — outside window, inside block, or quota consumed. Configured values are not included.

## 5. Phased Rollout

No feature flag; see the waiver in **Guiding Decisions**. Consequently there is no flag-removal phase. Every phase that touches an existing read path carries a test asserting the unconfigured path is byte-for-byte unchanged, which is this plan's substitute for a flag-off test.

---

### Phase 0 — Group-slot scoping on the availability tables

**Goal**: the schema and the default-exclude scoping exist, with zero behavior change anywhere. Ship value: none on its own — this is the foundation every later phase consumes, and it is separated because getting default-exclude wrong is the plan's highest-blast-radius failure.

**Feature flag**: none — see the waiver in **Guiding Decisions**. This phase is unreachable from any existing flow: no code writes the new column yet.

Changes:
1. [calendar_integration/models.py](calendar_integration/models.py): nullable group-slot foreign key on `AvailableTime` and `BlockedTime`, cascading on slot deletion.
2. Migration: add both columns nullable with no default so the change is metadata-only, add the foreign key constraints as not-valid and validate separately, and create the partial indexes concurrently. Hot tables — the lock-aware path is mandatory, not optional.
3. [calendar_integration/managers.py](calendar_integration/managers.py) and [calendar_integration/querysets.py](calendar_integration/querysets.py): default manager filters to base rows; explicit accessors for group-scoped and unscoped views.
4. [calendar_integration/admin.py](calendar_integration/admin.py): use the unscoped accessor so admin keeps showing every row.
5. Verify the assumption recorded in **Guiding Decisions**: confirm the occurrence SQL functions under [calendar_integration/migrations/sql/functions/](calendar_integration/migrations/sql/functions/) take row ids as input and select no rows themselves. If any does, bump its version to respect the scope column and note the added surface.

**Spec use-case**: shared scaffolding — no use-case yet.

Tests:
- **Unit**: [calendar_integration/tests/test_models.py](calendar_integration/tests/test_models.py) — default manager excludes group-scoped rows; explicit accessors return them; cascade on slot deletion removes them.
- **Integration**: [calendar_integration/tests/services/](calendar_integration/tests/services/) — the full existing availability and group test suites pass unchanged, and a group-scoped row inserted directly is invisible to every base read path (services, serializers, public API).

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Not the migration itself, which is routine, but the default-manager change reaches every consumer of two hot models and the blast radius of getting it wrong is silent incorrect availability.

**Review models**: reviewer Tier 4 — a leak here makes calendars bookable when they should not be, and the failure is silent rather than loud. The independent review runs on the most capable model. Fixer left on the project default.

**Reusable skills**: `add-migration` (lock-aware column addition on hot tables; couple with the `migration-author` agent).

**Acceptance**: both columns exist and are indexed, the full existing test suite passes with no edits to existing call sites, and a directly-inserted group-scoped row is returned by no base read path.

---

### Phase 0b — Organization week-start setting

**Goal**: organizations can declare whether their week starts Monday or Sunday. Ship value: none until quota lands; separated because it is a different app and can be reviewed and merged independently.

**Feature flag**: none — purely additive field with a database default; nothing reads it yet.

Changes:
1. [organizations/models.py](organizations/models.py): week-start choice field, database default Monday, following the `external_event_update_policy` precedent.
2. Migration adding the column with a database default.
3. Admin and the organization serializer: expose the field, admin-editable only.

**Spec use-case**: shared scaffolding — supports UC-2.

Tests:
- **Unit**: [organizations/tests/](organizations/tests/) — default is Monday for new and existing organizations; non-admins cannot change it.

**Suggested AI model**: Tier 1 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Single field, migration, admin registration, exact precedent in the same file.

**Reusable skills**: `add-migration`.

**Acceptance**: every existing organization reads Monday, admins can change it, and nothing else in the system consults it yet.

---

### Phase 1a — Group-scoped availability windows: writes

**Goal**: an admin or calendar owner can create, edit, and delete group-scoped availability windows through the service layer, audited and permission-gated, and a narrowing reports the bookings it orphans.

**Feature flag**: none. Reachable only through new service methods; no existing caller enters this code.

Changes:
1. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): write methods for group-scoped windows — create, update, delete — writing through the explicit group-scoped accessor.
2. Permission checks in [calendar_integration/services/calendar_permission_service.py](calendar_integration/services/calendar_permission_service.py): calendar owner within a group they can see, or org admin. A member must not learn a group exists from an error message.
3. Audit wiring following the pattern already established in [calendar_integration/services/booking_policy_service.py](calendar_integration/services/booking_policy_service.py) — all three operations, before/after values on update.
4. Orphaned-booking detection: on a narrowing, collect confirmed future bookings in that group for that calendar now outside the configuration and return them. Cancel nothing.
5. [calendar_integration/services/dataclasses.py](calendar_integration/services/dataclasses.py): the write result type carrying the warning.

**Spec use-case**: UC-1 (admin narrows a surgeon to operating days), UC-6 (admin tightens a window that orphans bookings).

Tests:
- **Unit**: [calendar_integration/tests/services/](calendar_integration/tests/services/) — create/update/delete; recurrence and per-window timezone round-trip; audit records emitted with diffs; owner-outside-group and non-owner both denied without disclosing the group.
- **Integration**: narrowing a window with confirmed future bookings returns them and modifies none of them; deleting the slot membership removes the windows.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Multi-file orchestration across service, permission, and audit layers with non-trivial permission branching.

**Reusable skills**: none — no clean match; the audit and permission patterns are followed from the existing booking-policy service.

**Acceptance**: windows can be written and read back through the service layer with full recurrence, every write produces an audit record, permission rules hold, and a narrowing returns its orphaned bookings without touching them.

---

### Phase 1b — Windows in discovery and booking validation

**Goal**: a calendar with group-scoped windows is offered only inside them, in that group only, and a booking that names it outside them is rejected.

**Feature flag**: none. **Self-gating**: a calendar with no group-scoped windows takes an early-out before any new work runs, so the unconfigured path is unchanged and issues no extra queries. This is the phase where goal 3 is proven.

Changes:
1. [calendar_integration/services/slot_engine.py](calendar_integration/services/slot_engine.py): fetch group-scoped windows for the search range and intersect them into the free-check, after base availability and before anything else.
2. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): `find_bookable_slots` and `check_group_availability` consume the intersected result. The early-out when no calendar in the group has group-scoped windows must skip the fetch entirely.
3. Booking and rescheduling validation: reject a selected calendar that is outside its window for the requested time, with the calendar identifier and rule type.
4. [calendar_integration/services/bookable_slots_service.py](calendar_integration/services/bookable_slots_service.py): confirm the single-calendar path is untouched — it has no group context and must not gain one.

**Spec use-case**: UC-1, UC-4 (patient books through the group), and the discovery half of UC-6.

Tests:
- **Integration**: [calendar_integration/tests/services/](calendar_integration/tests/services/) — the surgeon scenario end to end, including that the same calendar is unaffected in a second group; a window outside base availability yields no offered time; explicit booking outside the window is rejected with the right shape.
- **Integration — unchanged path**: for a group with no group-scoped configuration, discovery output is identical to the pre-change result **and** the query count is unchanged. This is the substitute for a flag-off test and is required.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Hot read path, an early-out that must be exactly right, and integration tests spanning discovery through booking.

**Review models**: reviewer Tier 4 — this phase modifies the main booking read path, and the zero-change guarantee is easy to assert and hard to actually hold. Fixer left on the project default.

**Reusable skills**: none.

**Acceptance**: a calendar with a Tuesday/Thursday window in one group is offered only Tuesdays and Thursdays there and on its full base availability elsewhere; a group with nothing configured produces byte-for-byte identical discovery output with no extra queries.

---

### Phase 1c — Windows on the internal REST surface

**Goal**: the first-party web app can manage group-scoped windows for a calendar within a slot.

**Feature flag**: none — new routes nested under the group slot; no existing route changes shape.

Changes:
1. [calendar_integration/serializers.py](calendar_integration/serializers.py), [calendar_integration/views.py](calendar_integration/views.py), [calendar_integration/routes.py](calendar_integration/routes.py): a viewset nested under the group slot, delegating to the Phase 1a service methods.
2. [calendar_integration/permissions.py](calendar_integration/permissions.py): route-level group-visibility gating.
3. The narrowing response carries the orphaned-booking warning.
4. Regenerate the OpenAPI schema.

**Spec use-case**: UC-1, UC-6 (their user-facing entry point).

Tests:
- **Integration**: [calendar_integration/tests/](calendar_integration/tests/) — full lifecycle through the endpoints; cross-organization and cross-group access denied; the warning appears in the narrowing response.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Serializer plus viewset plus route registration against an established pattern, delegating logic already built.

**Reusable skills**: `create-rest-endpoint`.

**Acceptance**: windows are manageable through the nested routes, the schema regenerates cleanly, and no existing route's shape changed.

---

### Phase 1d — Windows on the public API

**Goal**: partner integrations can read and batch-write group-scoped windows, idempotently and under the plan limit.

**Feature flag**: none — new operations; existing availability operations untouched.

Changes:
1. [calendar_integration/graphql.py](calendar_integration/graphql.py): a type for group-scoped windows.
2. [public_api/queries.py](public_api/queries.py) and [public_api/mutations.py](public_api/mutations.py): a query and a batch-upsert mutation, following the existing availability batch write's all-or-nothing and over-limit behavior exactly.
3. [public_api/permissions.py](public_api/permissions.py) and [public_api/constants.py](public_api/constants.py): resource mapping and scoping for the new operations.
4. Confirm group-scoped windows count toward the availability window limit through `only_user_authored`.

**Spec use-case**: UC-5 (upstream rostering system pushes windows).

Tests:
- **Integration**: [public_api/tests/](public_api/tests/) — batch upsert applies; identical replay is a no-op; a batch exceeding the plan limit is rejected whole with the existing over-limit body and creates nothing; cross-organization scoping holds; the existing availability operations' responses are unchanged.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Batch-upsert semantics interacting with entitlement enforcement, where partial application would be a data-integrity bug.

**Reusable skills**: `create-graphql-public-query`.

**Acceptance**: an integration can push a roster batch, replay it safely, and is blocked at the plan limit with nothing partially created; existing availability operations are byte-for-byte unchanged.

---

### Phase 2a — Group-scoped blocked time: writes and enforcement

**Goal**: a member can block time within one group without affecting any other group, and the block wins over any window.

**Feature flag**: none. Self-gating — no group-scoped blocks means the fetch is skipped.

Changes:
1. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): write methods for group-scoped blocks, mirroring Phase 1a including audit and permissions.
2. [calendar_integration/services/slot_engine.py](calendar_integration/services/slot_engine.py): blocks applied after base availability and before windows, per the spec's resolution order — a block removes time regardless of what a window says.
3. Booking and rescheduling validation rejects a calendar inside a block, with the rule type.

**Spec use-case**: UC-3 (member blocks one week for one activity).

Tests:
- **Unit**: write lifecycle, audit, permissions.
- **Integration**: a group-scoped block hides the calendar in that group and nowhere else; a block overlapping a group-scoped window wins; the unconfigured path is unchanged with no extra queries.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Close mirror of Phase 1a and 1b with the precedent freshly established in the same files; step up to Tier 3 if the resolution-order change in the slot engine proves more invasive than a filter insertion.

**Reusable skills**: none.

**Acceptance**: a block created in one group removes time there only, beats an overlapping window, and leaves unconfigured groups unchanged.

---

### Phase 2b — Blocks on the REST and public surfaces

**Goal**: blocks are manageable and readable everywhere windows are.

**Feature flag**: none — new routes and operations.

Changes:
1. REST viewset, serializer, and nested route mirroring Phase 1c; regenerate the schema.
2. Public API query and batch-upsert mutation mirroring Phase 1d, with resource mapping and scoping.

**Spec use-case**: UC-3 (its entry points).

Tests:
- **Integration**: lifecycle through both surfaces; scoping and permission denials; batch idempotency and over-limit rejection.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Direct application of the pattern established in Phases 1c and 1d.

**Reusable skills**: `create-rest-endpoint`, `create-graphql-public-query`.

**Acceptance**: blocks have full parity with windows on both surfaces.

---

### Phase 2c — Meter all blocked time

**Goal**: blocked time counts toward the availability window plan limit, base rows included, so one rule covers every authored time window.

**Feature flag**: none, and this phase is **not self-gating** — it changes the meaning of the counter for every organization rather than only for configured ones. With the product pre-customer there is no installed base to affect, so this is a rule change made at its cheapest moment rather than a breaking one. It stays in its own phase for reviewability and independent revert.

Changes:
1. [payments/services/entitlement_service.py](payments/services/entitlement_service.py): the availability window counter includes blocked time — base and group-scoped — alongside availability. Apply the same user-authored filtering the availability counter already uses, so recurrence exceptions and series splits do not inflate the count; if `BlockedTimeQuerySet` lacks that filter, add its equivalent.
2. Confirm the existing limit-warning notification path fires for organizations pushed near or over their limit by the change.

**Spec use-case**: none — billing rule change required by the spec's metering decision.

Tests:
- **Unit**: [payments/tests/services/](payments/tests/services/) — the counter includes base and group-scoped blocked time; recurrence exceptions and split series do not inflate it.
- **Integration**: an organization over the new limit is blocked from creating further windows and receives the existing over-limit response; the limit-warning notification fires.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). A counter change against an established pattern, with the subtlety concentrated in the user-authored filtering.

**Reusable skills**: none.

**Acceptance**: the counter reports base plus group-scoped blocked time plus availability, without inflation from recurrence exceptions, and an organization over its limit hits the existing over-limit path rather than any new error.

---

### Phase 3a — Quota model and period-counting function

**Goal**: quota rules can be stored and per-period booking counts can be computed. Ship value: none on its own — the model and the counting primitive that Phase 3b consumes.

**Feature flag**: none — new model, new SQL function, nothing reads them yet.

Changes:
1. [calendar_integration/models.py](calendar_integration/models.py): the quota rule model with its manager and queryset; unique per (calendar, slot, period); cascade with the slot membership.
2. Migration for the model.
3. A versioned raw-SQL function under [calendar_integration/migrations/sql/functions/](calendar_integration/migrations/sql/functions/) returning per-calendar per-period counts of live bookings made through a given group, honoring the organization's week start, plus its migration and the Django function wrapper in [calendar_integration/database_functions.py](calendar_integration/database_functions.py).
4. [calendar_integration/factories.py](calendar_integration/factories.py): a factory for the new model.

**Spec use-case**: shared scaffolding — supports UC-2.

Tests:
- **Unit**: model constraints; cascade behavior.
- **Integration**: the counting function against day, week, and month periods; Monday and Sunday week starts; cancelled bookings excluded; a booking rescheduled across a period boundary counted in the new period only; bookings made outside the group excluded.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). New versioned SQL function plus model plus migration, with period-boundary and timezone edges that are easy to get subtly wrong.

**Reusable skills**: `add-model`, `create-postgres-function`, `add-migration`.

**Acceptance**: quota rules persist with their constraints, and the counting function returns correct counts across all three periods, both week starts, and the cancellation and reschedule edges.

---

### Phase 3b — Quota in discovery and booking validation

**Goal**: a calendar that has consumed its quota for a period stops being offered for that period and is rejected if named directly.

**Feature flag**: none. Self-gating — no quota rules in the group means the counting query is never issued.

Changes:
1. [calendar_integration/services/slot_engine.py](calendar_integration/services/slot_engine.py) and [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): one counting call per discovery covering the whole search window, bucketed by period, consulted per candidate as a lookup. All rules for a calendar must pass.
2. Booking and rescheduling validation rejects a calendar with no headroom, naming the quota as the rule type.

**Spec use-case**: UC-2 (member caps their own weekly load), and the quota half of UC-4.

Tests:
- **Integration**: a calendar at its cap is not offered for that period and is offered again the following period; cancelling a booking makes it available again with no further action; two rules (daily and weekly) both enforced; explicit booking past the cap rejected with the right shape; the unconfigured path unchanged with no extra queries.

**Suggested AI model**: Tier 3 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Hot read path plus multi-rule evaluation plus period bucketing, with the performance risk the spec flagged concentrated here.

**Review models**: reviewer Tier 4 — quota evaluation lands on the main discovery loop, and the phase must add at most one query per discovery regardless of candidate count. A per-candidate query here is a production performance regression that tests will not catch. Fixer left on the project default.

**Reusable skills**: none.

**Acceptance**: a calendar capped at three a week disappears from that week once three are booked and returns when one is cancelled; discovery issues one counting query per call, not one per candidate; unconfigured groups are unchanged.

---

### Phase 3c — Quota rules on the REST and public surfaces

**Goal**: quota rules are manageable and readable everywhere windows and blocks are.

**Feature flag**: none — new routes and operations.

Changes:
1. REST viewset, serializer, and nested route; regenerate the schema.
2. Public API query and mutation with resource mapping and scoping. Quota rules are not metered, so no entitlement check applies to their creation.

**Spec use-case**: UC-2 (its entry points).

Tests:
- **Integration**: lifecycle through both surfaces; multiple rules per calendar and slot; the uniqueness constraint surfaced as a validation error, not a server error; permission and scoping denials.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Established pattern from the four preceding surface phases.

**Reusable skills**: `create-rest-endpoint`, `create-graphql-public-query`.

**Acceptance**: quota rules have full parity with windows and blocks on both surfaces, and no entitlement check gates their creation.

---

### Phase 4 — Client handoff document

**Goal**: the web SPA and partner integration teams have a written contract for everything added, without reading this repository.

**Feature flag**: none — documentation only.

Changes:
1. Generate the handoff covering every new REST route and public API operation across Phases 1c, 1d, 2b, and 3c: request and response shapes, auth, errors including the rejection reason shape, and the orphaned-booking warning field.
2. Record explicitly that the existing availability operations are unchanged, and that the blocked-time metering change from Phase 2c alters plan-limit consumption for existing integrations.

**Spec use-case**: none — delivery artifact.

Tests: none — documentation.

**Suggested AI model**: Tier 2 (IDs in [.claude/skills/plan-feature/resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Reading a branch diff and writing accurate contract documentation.

**Reusable skills**: `handoff-to-client`.

**Acceptance**: the handoff document covers every added operation and both breaking-change notes, and a client implementer can build against it without opening this repository.

## 6. Risk & Rollout Notes

**No feature flag.** Justified in **Guiding Decisions**. The protection is structural: every phase touching an existing read path early-outs when no group-scoped configuration exists, and carries a test asserting identical output and unchanged query count on that path. If any phase cannot hold that assertion, stop and reconsider rather than weakening the test.

**Migration safety.** Phase 0 adds columns to two hot tables. Add them nullable with no default so the change is metadata-only; add the foreign keys as not-valid and validate in a separate step; create the partial indexes concurrently. Phase 0b and Phase 3a are ordinary migrations on cold paths. Couple Phase 0 with the `migration-author` agent.

**The default-manager change is the highest-blast-radius edit in the plan.** It silently alters what every existing consumer of two hot models sees. A mistake does not raise — it just returns the wrong availability. Phase 0's acceptance requires the existing suite to pass with no edits to existing call sites; treat any needed edit as a signal the scoping is wrong, not as a task.

**Phase 2c changes a billing rule, and is cheap only because the product is pre-customer.** Metering all blocked time would raise reported usage for any organization that has authored some — with none on the product, it affects nobody. This is the reason to do it now rather than later: the same change costs a measurement pass, a limit-raising exercise, and customer communication once there is an installed base. **If customers onboard before this phase merges, revisit it** — the declined measure-first path becomes the right one at that point.

**Query-plan risk on discovery.** Phases 1b, 2a, and 3b all add fetches to the main booking read path. Each must issue a fixed number of queries per discovery call, independent of candidate count — the existing engine already batches this way and the pattern must be preserved. Phase 3b is the one most likely to regress, since a naive implementation counts per candidate.

**No backfill.** Nothing needs backfilling: the new columns default to null, which is exactly today's behavior, and quota is derived on read rather than stored.

**Rollback.** Every phase is independently reversible. Phases 1a through 3c revert as ordinary code reverts, since no existing behavior depends on them. Phase 2c reverts by restoring the counter. Phase 0's columns can be left in place on a revert — they are nullable and unread — so the migration does not need to be reversed under pressure.

**Deploy ordering.** No cross-repo producer, so no ordering constraint outside this repository. Client teams consume Phase 4's handoff after the API phases have merged; nothing in this plan blocks on them.

## 7. Open Questions

The spec resolved every question raised during its own drafting, and Step 0 of this plan resolved the implementation decisions. Two items are carried deliberately rather than unresolved:

1. **Does the occurrence SQL machinery need version bumps?** Recorded in **Guiding Decisions** as an assumption with a verification step in Phase 0 rather than a decision. Recommended default: assume no change is needed, since the functions take row ids and scoping happens in the caller. If Phase 0 finds otherwise, the phase grows by however many function versions are affected, and that should be reported before continuing rather than absorbed silently. Answerable in Phase 0 itself.

2. **Does `BlockedTimeQuerySet` need a user-authored filter?** The availability counter avoids over-reporting by excluding rows that recurrence editing inserts. Blocked time has no such filter today because it was never metered. Recommended default: add the equivalent in Phase 2c. If the recurrence models differ enough that the filter is not a direct translation, that phase grows. Answerable by whoever implements Phase 2c.

## 8. Touch List

**Phase 0** — edited: [calendar_integration/models.py](calendar_integration/models.py), [calendar_integration/managers.py](calendar_integration/managers.py), [calendar_integration/querysets.py](calendar_integration/querysets.py), [calendar_integration/admin.py](calendar_integration/admin.py). New: migration under @calendar_integration/migrations/.

**Phase 0b** — edited: [organizations/models.py](organizations/models.py), [organizations/admin.py](organizations/admin.py), [organizations/serializers.py](organizations/serializers.py). New: migration under @organizations/migrations/.

**Phase 1a** — edited: [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py), [calendar_integration/services/calendar_permission_service.py](calendar_integration/services/calendar_permission_service.py), [calendar_integration/services/dataclasses.py](calendar_integration/services/dataclasses.py), [calendar_integration/exceptions.py](calendar_integration/exceptions.py).

**Phase 1b** — edited: [calendar_integration/services/slot_engine.py](calendar_integration/services/slot_engine.py), [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py).

**Phase 1c** — edited: [calendar_integration/serializers.py](calendar_integration/serializers.py), [calendar_integration/views.py](calendar_integration/views.py), [calendar_integration/routes.py](calendar_integration/routes.py), [calendar_integration/permissions.py](calendar_integration/permissions.py), @schema.yml.

**Phase 1d** — edited: [calendar_integration/graphql.py](calendar_integration/graphql.py), [public_api/queries.py](public_api/queries.py), [public_api/mutations.py](public_api/mutations.py), [public_api/permissions.py](public_api/permissions.py), [public_api/constants.py](public_api/constants.py).

**Phase 2a** — edited: [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py), [calendar_integration/services/slot_engine.py](calendar_integration/services/slot_engine.py).

**Phase 2b** — edited: the same REST and public API files as Phases 1c and 1d, plus @schema.yml.

**Phase 2c** — edited: [payments/services/entitlement_service.py](payments/services/entitlement_service.py), [calendar_integration/querysets.py](calendar_integration/querysets.py).

**Phase 3a** — edited: [calendar_integration/models.py](calendar_integration/models.py), [calendar_integration/managers.py](calendar_integration/managers.py), [calendar_integration/querysets.py](calendar_integration/querysets.py), [calendar_integration/database_functions.py](calendar_integration/database_functions.py), [calendar_integration/factories.py](calendar_integration/factories.py), [calendar_integration/admin.py](calendar_integration/admin.py). New: model migration and SQL function migration under @calendar_integration/migrations/, function source under @calendar_integration/migrations/sql/functions/.

**Phase 3b** — edited: [calendar_integration/services/slot_engine.py](calendar_integration/services/slot_engine.py), [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py).

**Phase 3c** — edited: the same REST and public API files as the earlier surface phases, plus @schema.yml.

**Phase 4** — new: handoff document under @.vinta-ai-workflows/client-handoffs/.

Test files accompany every phase under @calendar_integration/tests/, @organizations/tests/, @payments/tests/, and @public_api/tests/, as named in each phase's Tests section.
