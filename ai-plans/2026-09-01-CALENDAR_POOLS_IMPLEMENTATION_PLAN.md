# Calendar Pools — Implementation Plan

No `..._SPEC.md` sibling exists for this feature. The decisions below were settled in a Step 0 interrogation with the requester rather than derived from a spec; where a phase would normally name a spec use-case, it names the decision it implements instead. If a spec is written later, it should be reconciled against the **Guiding Decisions** table rather than the phases.

## 1. Goals

1. An organization can define a **CalendarPool** — a named, reusable roster of calendars ("Nurses", "Consult Rooms") — and attach it to the slots of any number of `CalendarGroup`s, so one roster edit propagates everywhere it is used.
2. A slot's bookable roster is the **union** of its own inline calendars and the calendars of every pool attached to it, with a calendar present in both surviving the detachment of either source.
3. Removing a calendar from a roster — inline or via a pool — never destroys configuration and never fails because of existing bookings. Existing events keep the calendars they hold; roster membership is enforced only when a calendar is **added** to an event.
4. An event holding a calendar that has since left its slot's roster is identifiable through the API, so the edit UI can warn and ops can sweep for them.
5. Groups and slots with no pools attached behave byte-for-byte as they do today.

**Non-goals:**

- Renaming `CalendarGroup` to `AppointmentType` (or renaming `CalendarGroupSlot`). Discussed and deliberately deferred — it breaks the public GraphQL contract, the `calendar_groups` billing key, and two versioned Postgres functions, and buys nothing this feature needs.
- Availability windows, blocked time, or quota rules defined **on a pool** and inherited by slots. Scoped rules stay keyed on `(group_slot, calendar)` exactly as today.
- Pool-level booking policies, pool-level `accepts_public_scheduling`, or any pool attribute beyond name, description, and roster.
- Metering pools as a billable resource. No new key in `payments/seams/resource_keys.py`, no plan-catalog change.
- Nested or hierarchical pools (a pool containing another pool).
- Automatic cancellation, rescheduling, or attendee notification for events left holding a calendar that departed a roster.
- Backfilling existing slots into pools. Every existing slot keeps its inline roster untouched.
- A feature-flag mechanism. See the waiver in **Guiding Decisions**.
- Optimistic locking on concurrent pool edits — last-write-wins, matching the existing group-write contract.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Model name** | `CalendarPool`, not `CalendarGroup` or `CalendarRoster`. The existing slot docstrings already call this concept "a pool of candidate calendars" twice, so the name is the codebase's own vocabulary rather than a new one. It also stays correct if `CalendarGroup` is ever renamed to `AppointmentType`, so the choice never has to be revisited. |
| **Slot ↔ pool arity** | Many-to-many. A slot can union "Nurses" and "Senior Nurses" without duplicating rosters. A nullable FK was considered and rejected as an artificial ceiling on a feature whose entire point is reuse. |
| **Roster composition** | Union of inline calendars and pool calendars, deduplicated. Not exclusive-or: a slot that is "the Nurses pool plus Dr. Silva" is the motivating case, and forcing that into a one-off pool defeats reuse. |
| **Roster resolution** | **Projected into `CalendarGroupSlotMembership`**, not computed on read. A nullable `source_pool` column marks each row's origin; attaching a pool writes derived rows, detaching deletes exactly those. Chosen because nine call sites reach the roster through the `memberships` relation — two of them permission checks and one a correlated subquery inside an availability annotation — and repointing all nine correctly is a larger correctness risk than one reconcile function with tests. This repo also has no SQL-view precedent (no `managed = False` model anywhere), so the computed-view alternative would introduce a pattern with nothing to copy. The accepted cost is projection drift, mitigated below. |
| **Drift mitigation** | Every write that can change a resolved roster goes through one reconcile entry point in `CalendarGroupService`, inside `transaction.atomic()`. A management command recomputes the projection from scratch and reports differences, so drift is detectable and repairable without a migration. |
| **Uniqueness under projection** | `UNIQUE(slot_fk, calendar_fk, source_pool_fk)` cannot be a plain constraint: Postgres treats NULLs as distinct, so two inline rows for the same pair would both be accepted. The inline case needs `NULLS NOT DISTINCT` or a partial unique index on `source_pool_fk IS NULL`. Called out because getting this wrong silently permits duplicate inline rows, which would corrupt `required_count` satisfaction counting. |
| **Roster removal semantics** | Lenient, and unified across inline and pool removal. Removal always succeeds; `_ensure_no_future_selections` leaves the removal path. The same user-facing action ("drop this nurse from the candidates") must not behave differently based on plumbing the user cannot see, which is what a strict-inline / lenient-pool split would have produced. |
| **Grandfathered selections** | Existing `CalendarEventGroupSelection` rows are never touched by a roster change, past or future. Roster membership is validated only against calendars being **added** to an event; calendars already selected pass through untouched on update and reschedule. |
| **Scoped-row survival** | The group-scoped `AvailableTime`, `BlockedTime`, and `CalendarGroupSlotQuotaRule` rows for a departed calendar are **kept and keep enforcing**. So a reschedule of a grandfathered booking still respects that calendar's cap and its slot-scoped windows, and nothing is lost if the calendar rejoins. `_delete_group_scoped_rows_for_removed_calendars` is deleted, not merely bypassed — under a shared pool it would let one edit destroy per-slot configuration in groups the editor never opened, unrecoverably. |
| **Staleness definition** | A selection is stale when no `CalendarGroupSlotMembership` row exists for its `(slot, calendar)` pair, regardless of source. Because the union is projected into that table, this one predicate stays correct before and after pools exist, and needs no union logic of its own. |
| **Staleness surfaces** | Both a computed per-selection boolean, so the edit UI gets the warning inside data it already loads, and a standalone query returning stale `(event, slot, calendar)` triples for ops sweeps. |
| **Pool deletion** | `PROTECT`. Refused while any slot references the pool, with the referencing groups named in the error. Mirrors `delete_group`'s existing refuse-when-referenced posture. Cascading would silently empty rosters in groups the deleter never looked at. |
| **API surfaces** | Both internal REST and public GraphQL in v1, matching `CalendarGroup`, which has a `CalendarGroupViewSet` and public GraphQL queries. Splitting them across a version boundary would leave the Web SPA managing pools over GraphQL while it manages groups over REST. |
| **Visibility scoping** | `org_wide` and `scoped_admin` get full CRUD. `scoped_member` reads only pools containing a calendar they own. A missing or inactive membership resolves to an empty queryset — fail closed, matching `scoped_calendar_group_queryset`. |
| **Billing** | Unmetered. A pool is a convenience over a roster an organization can already build inline, so it adds no independently billable capability. Revisit if pools grow attributes of their own. |
| **No feature flag — waiver** | This project has no feature-flag library, and the calendar-group-scoped-availability plan already took the same waiver. It holds here for a different reason per phase. Phases 0, 3, 4, 5 and 6 are **self-gating**: a slot with no pools attached resolves through the exact code path it does today, so nothing changes until an admin deliberately attaches one, and each of those phases carries a test asserting the no-pool path is unchanged. Phases 1 and 2 are **not** self-gating and are handled as a documented behavior change — see **Risk & Rollout Notes**. |
| **Behavior-change posture** | The lenient-removal change is strictly *less* destructive than today's: it stops deleting configuration and stops rejecting operations. It cannot corrupt existing data, and no booking that works today stops working. The exposure is a client that treated the rejection as validation, which is a client-communication problem, not a data-safety one, so it ships through `handoff-to-client` rather than behind infrastructure built for this one change. |
| **Phase granularity** | Bundled by related use-case, per the Step 0 answer. CRUD for one surface is one phase rather than four; the two staleness surfaces are separate phases because they serve different consumers and are independently useful. |

## 3. Data Model Changes

### 3.1 New `CalendarPool`

An organization-scoped model in [calendar_integration/models.py](calendar_integration/models.py), following the `CalendarGroup` shape directly above it: `SingleOrganizationModelMixin, SafeRelationNullInitMixin, BaseModel`, with `name`, `description`, and a `UniqueConstraint` on `(organization, name)` mirroring `calendargroup_unique_name_per_org`.

The calendar roster is a `ManyToManyField` to `Calendar` through `CalendarPoolMembership`, matching how `CalendarGroupSlot.calendars` is declared.

A `CalendarPoolManager` and `CalendarPoolQuerySet` land in [calendar_integration/managers.py](calendar_integration/managers.py) and [calendar_integration/querysets.py](calendar_integration/querysets.py). The queryset carries `only_member_of(membership_user_id)`, the pool analogue of `CalendarGroupQuerySet.only_member_of` at [querysets.py:842-856](calendar_integration/querysets.py#L842-L856) — pools where the user owns at least one roster calendar, `distinct()` because a user may own several.

### 3.2 New `CalendarPoolMembership`

Through model linking a `Calendar` to a `CalendarPool`, using `OrganizationSafeForeignKey` on both sides with `on_delete=CASCADE`, and unique on `(pool_fk, calendar_fk)`. Directly parallel to `CalendarGroupSlotMembership` at [models.py:394-423](calendar_integration/models.py#L394-L423).

### 3.3 New `CalendarGroupSlotPool`

Through model for the slot ↔ pool many-to-many. `OrganizationSafeForeignKey` to `CalendarGroupSlot` (`CASCADE` — deleting a slot drops its attachments) and to `CalendarPool` (**`PROTECT`** — this FK is what enforces the refuse-when-referenced deletion rule at the schema level rather than in application code). Unique on `(slot_fk, pool_fk)`.

`CalendarGroupSlot` gains `pools = ManyToManyField(CalendarPool, through="CalendarGroupSlotPool", related_name="group_slots")`.

### 3.4 `CalendarGroupSlotMembership.source_pool`

A nullable `OrganizationSafeForeignKey` to `CalendarPool` on the existing through model, `on_delete=CASCADE`. `NULL` means the row is inline — every row that exists today. Non-null means the row was projected from that pool.

The existing `calendargroupslotmembership_unique_slot_calendar` constraint on `(slot_fk, calendar_fk)` must be **replaced**, because the union deliberately allows the same calendar to be present from more than one source. The replacement is unique on `(slot_fk, calendar_fk, source_pool_fk)` with NULL treated as a value — see the uniqueness note in **Guiding Decisions**. This is a constraint swap on a table with production rows and is the single most delicate migration in the plan.

An index on `source_pool_fk` supports the detach path, which deletes by `(slot, source_pool)`.

### 3.5 Counting under duplicate calendars

`CalendarGroupQuerySet.only_groups_bookable_in_ranges` counts roster satisfaction with `Count("memberships", filter=..., distinct=True)` at [querysets.py:911-916](calendar_integration/querysets.py#L911-L916). That counts distinct membership **rows**, which over-counts once a calendar can appear twice under two sources — a slot needing two calendars would look satisfied by one calendar present both inline and via a pool. It must become a distinct count over `memberships__calendar_fk_id`.

The same reasoning applies to `required_count` validation in `CalendarGroupService`, which compares selection counts against roster contents.

### 3.6 Type plumbing

`CalendarGroupSlotInputData` in [calendar_integration/services/dataclasses.py:329-336](calendar_integration/services/dataclasses.py#L329-L336) gains `pool_ids: list[int]` alongside its existing `calendar_ids`, defaulting to empty so every existing caller keeps compiling and behaving identically.

New `CalendarPoolInputData` for pool writes, and a `StaleSelection` dataclass carrying `(event_id, slot_id, calendar_id)` for the ops query.

`CalendarPoolVirtualModel` and `CalendarPoolMembershipVirtualModel` join [calendar_integration/virtual_models.py](calendar_integration/virtual_models.py); `CalendarGroupSlotVirtualModel` at [virtual_models.py:90-96](calendar_integration/virtual_models.py#L90-L96) gains `pools`.

## 4. API Design

### 4.1 Internal REST — pool CRUD

A `CalendarPoolViewSet` registered in [calendar_integration/routes.py](calendar_integration/routes.py) beside `CalendarGroupViewSet`, built on `VintaScheduleModelViewSet`. Standard list / retrieve / create / update / destroy, organization-scoped, with a filterset supporting filtering by member calendar id — the pool analogue of the `slots__memberships__calendar_fk_id` filter at [filtersets.py:220](calendar_integration/filtersets.py#L220).

Write payload accepts `calendar_ids: list[int]` and replaces the roster wholesale, matching how `CalendarGroupSlotSerializer` handles `calendar_ids` today. Delete returns a 409 naming the referencing groups when the pool is attached to any slot.

### 4.2 Internal REST — attaching pools to slots

`CalendarGroupSlotSerializer` at [serializers.py:2964-2989](calendar_integration/serializers.py#L2964-L2989) gains `pool_ids` (write-only, optional, defaults to unchanged) and `pools` (read-only, nested). Omitting `pool_ids` on a group update leaves attachments untouched, so every existing client payload keeps its current meaning — this is the omit-versus-empty-list distinction, and empty list means detach all.

### 4.3 Internal REST — stale selections

A read-only collection returning stale `(event, slot, calendar)` triples for a group, optionally bounded by a date window. Gated by the same permission as viewing the group.

### 4.4 Public GraphQL — pools

`CalendarPoolGraphQLType` in [calendar_integration/graphql.py](calendar_integration/graphql.py) with `calendarPool` / `calendarPools` queries and create / update / delete mutations registered in `public_api/queries.py` and `public_api/mutations.py`.

Requires a new `PublicAPIResources.CALENDAR_POOL` value in [public_api/constants.py](public_api/constants.py) and entries for every new field in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` at [public_api/permissions.py:29](public_api/permissions.py#L29). A field absent from that mapping is a scope-check gap, so the phase's acceptance asserts every new field name appears in it.

The pool's `calendars` resolver applies owner scoping the same way `CalendarGroupSlotGraphQLType.calendars` does at [graphql.py:829-831](calendar_integration/graphql.py#L829-L831), so a scoped token cannot read a sibling team's roster through a second hop.

### 4.5 Public GraphQL — staleness

`CalendarEventGroupSelectionGraphQLType` at [graphql.py:884](calendar_integration/graphql.py#L884) gains a computed `isInCurrentRoster` boolean, resolved from prefetched roster data rather than per-selection queries. `CalendarEventGroupSelectionSerializer` at [serializers.py:3077](calendar_integration/serializers.py#L3077) gains the REST equivalent.

## 5. Phased Rollout

### Phase 0 — Add the CalendarPool model and its roster

**Goal**: an organization can have named calendar pools that exist in the database and the admin, and nothing else in the system reads them yet. Ship value: none on its own — this is the foundation every later phase consumes. Justified as its own phase because it is pure additive scaffolding with zero blast radius, so it merges and reverts on its own.

**Feature flag**: none — purely additive surface. Two new tables no existing code reads or writes.

Changes:
1. [calendar_integration/models.py](calendar_integration/models.py): `CalendarPool` and `CalendarPoolMembership` per **Data Model Changes**, placed after the `CalendarGroupSlot` family.
2. [calendar_integration/managers.py](calendar_integration/managers.py) and [calendar_integration/querysets.py](calendar_integration/querysets.py): `CalendarPoolManager` / `CalendarPoolQuerySet` with `only_member_of`, plus the membership manager.
3. [calendar_integration/admin.py](calendar_integration/admin.py): register both, with the roster-count annotation pattern used for slots at [admin.py:396](calendar_integration/admin.py#L396).
4. [calendar_integration/factories.py](calendar_integration/factories.py): `CalendarPoolFactory`, `CalendarPoolMembershipFactory`.
5. [calendar_integration/virtual_models.py](calendar_integration/virtual_models.py): `CalendarPoolVirtualModel`, `CalendarPoolMembershipVirtualModel`.
6. One migration creating both tables and their constraints.

Implements: **Guiding Decisions → Model name**, and the storage half of goal 1.

Tests:
- **Unit**: `calendar_integration/tests/test_calendar_pool_models.py` — org-scoped uniqueness of `(organization, name)`; membership uniqueness; cross-organization safe-relation behavior.
- **Unit**: `calendar_integration/tests/test_calendar_pool_querysets.py` — `only_member_of` returns pools where the user owns a roster calendar, excludes others, dedupes when the user owns several in one pool.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Six files, but every one has an exact precedent in the `CalendarGroup` family immediately adjacent in the same modules.

**Reusable skills**: `add-model`, `add-migration`.

Acceptance: a `CalendarPool` with a roster can be created through the Django admin and through `CalendarPoolFactory`, is invisible to every existing query, and the full existing test suite passes unchanged.

---

### Phase 1 — Make roster removal non-destructive

**Goal**: removing a calendar from a slot's roster always succeeds, keeps every existing event's calendar selections intact, and preserves that calendar's group-scoped windows, blocked time, and quota rules. This is a deliberate behavior change to an existing flow and is sequenced early because it is the only part of the feature that needs client communication, which is the slowest-moving dependency in the plan.

**Feature flag**: none — waived. This phase is **not** self-gating, and the waiver rests on the change being strictly less destructive than current behavior: it removes a rejection and removes a deletion. No operation that succeeds today fails afterwards, and no data that survives today is destroyed afterwards. See **Risk & Rollout Notes** for the rollback path.

Changes:
1. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): `_reconcile_slot` at [line 513](calendar_integration/services/calendar_group_service.py#L513) stops calling `_ensure_no_future_selections` and stops calling `_delete_group_scoped_rows_for_removed_calendars` for the three scoped models. It now only deletes the `CalendarGroupSlotMembership` rows themselves.
2. Same module: delete `_delete_group_scoped_rows_for_removed_calendars` at [line 473](calendar_integration/services/calendar_group_service.py#L473) and `_ensure_no_future_selections`, along with any exception class no longer raised. Deleting rather than leaving them unreferenced, so a future change cannot quietly re-enable the destructive path.
3. Same module: selection validation at [line 3010](calendar_integration/services/calendar_group_service.py#L3010) changes from "every selected calendar must be in the roster" to "every calendar being **added** must be in the roster". On create, that is all of them, so booking behavior is unchanged. On update and reschedule, calendars already recorded on the event pass through regardless of current roster membership.
4. Update the `CalendarGroupSlotQuotaRule` docstring at [models.py:426-451](calendar_integration/models.py#L426-L451), which currently documents the deleted cleanup as the cascade story.

Implements: **Guiding Decisions → Roster removal semantics**, **Grandfathered selections**, **Scoped-row survival**; goal 3.

Tests:
- **Integration**: `calendar_integration/tests/services/test_calendar_group_service_lenient_removal.py` — removing a calendar with future bookings succeeds; the bookings keep their selections; the calendar's group-scoped `AvailableTime`, `BlockedTime`, and `CalendarGroupSlotQuotaRule` rows still exist afterwards and are still enforced on a subsequent reschedule of a grandfathered booking; re-adding the calendar restores it with its configuration intact.
- **Integration**: same file — updating an event to add a calendar **not** in the roster is still rejected; updating an event that already holds a departed calendar, without adding anything, succeeds.
- **Regression**: the existing group-service and group-scoped-availability suites must be updated where they assert the old rejection or the old deletion, and every such edit reviewed as an intentional contract change rather than a test fix.

**Suggested AI model**: Tier 3. Deletions are mechanical but the selection-validation change is a semantic split (added versus retained) inside booking and reschedule paths, and it must be provably inert on the create path.

**Review models**: reviewer Tier 4 — this phase removes a guard rail and a cleanup routine from the booking path, and the failure mode it must not introduce is a calendar becoming bookable through a slot it was never in. That is a security-adjacent correctness property worth the strongest independent review in the plan. Fixer stays on the project default.

**Reusable skills**: `handoff-to-client` — generate the API-change document for the Web SPA and partner integrations covering the removed rejection.

Acceptance: removing a calendar from a slot roster that has future bookings returns success, those bookings are unchanged, that calendar's scoped windows and quota rules still exist and still apply, and adding a non-roster calendar to an event is still rejected.

---

### Phase 2 — Surface stale calendar selections on events

**Goal**: any client loading an event can tell, per selection, whether that calendar is still in its slot's roster, so the edit UI can warn instead of silently presenting a calendar the user cannot re-add. Directly mitigates the state Phase 1 makes reachable.

**Feature flag**: none — purely additive read-only field on two existing types. Existing clients that do not request it see identical responses.

Changes:
1. [calendar_integration/graphql.py](calendar_integration/graphql.py): `CalendarEventGroupSelectionGraphQLType` at [line 884](calendar_integration/graphql.py#L884) gains `isInCurrentRoster`, resolved from roster data prefetched alongside the selections rather than one query per selection. The existing `group_selections` resolver at [line 463](calendar_integration/graphql.py#L463) already batches; the roster lookup joins that batch.
2. [calendar_integration/serializers.py](calendar_integration/serializers.py): `CalendarEventGroupSelectionSerializer` at [line 3077](calendar_integration/serializers.py#L3077) gains the REST equivalent as a read-only field.
3. [calendar_integration/virtual_models.py](calendar_integration/virtual_models.py): `CalendarEventGroupSelectionVirtualModel` at [line 105](calendar_integration/virtual_models.py#L105) prefetches what the field needs, so the REST list path does not regress into N+1.

Implements: **Guiding Decisions → Staleness definition**, the per-selection half of **Staleness surfaces**; goal 4.

Tests:
- **Integration**: `calendar_integration/tests/test_stale_selection_flag.py` — the flag is true for a selection whose calendar is still rostered, false after that calendar is removed, and true again after it is re-added.
- **Integration**: same file — a query-count assertion proving the field is constant-query with respect to the number of selections, on both the REST and GraphQL paths.

**Suggested AI model**: Tier 2. Two computed read-only fields against established serializer and strawberry patterns; the only real content is the prefetch.

**Reusable skills**: `create-graphql-public-query` for the GraphQL field.

Acceptance: an event holding a calendar removed from its slot reports that selection as not in the current roster on both REST and GraphQL, and the selection list issues the same number of queries for one selection as for twenty.

---

### Phase 3 — Attach pools to slots and project the roster

**Goal**: attaching a pool to a slot makes that pool's calendars bookable through the slot, and detaching removes exactly those without touching inline calendars. This is the core of the feature.

**Feature flag**: none — self-gating. A slot with no pools attached has no projected rows and resolves through the identical code path as today.

Changes:
1. [calendar_integration/models.py](calendar_integration/models.py): `CalendarGroupSlotPool` through model and the `CalendarGroupSlot.pools` m2m per **Data Model Changes**; `source_pool` on `CalendarGroupSlotMembership`.
2. Migration: create the through table, add the nullable column, and **swap** `calendargroupslotmembership_unique_slot_calendar` for the three-column constraint with NULL treated as a value. Existing rows take `source_pool = NULL` and stay valid under both the old and new constraint, so the swap is safe in either order; the migration still adds the new constraint before dropping the old one.
3. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): one `_reconcile_slot_pools(slot, pool_ids)` entry point, called inside the existing `transaction.atomic()`. Attaching projects one membership row per pool calendar with `source_pool` set; detaching deletes rows matching `(slot, source_pool)` only. Inline rows are never read or written by this path.
4. Same module: `_reconcile_slot` handles `pool_ids` from `CalendarGroupSlotInputData`, treating an omitted value as "leave attachments unchanged".
5. [calendar_integration/querysets.py](calendar_integration/querysets.py): `only_groups_bookable_in_ranges` at [line 911](calendar_integration/querysets.py#L911) counts distinct `memberships__calendar_fk_id` rather than distinct membership rows, per **Counting under duplicate calendars**.
6. Same fix wherever `CalendarGroupService` compares selection counts to roster size for `required_count`.
7. A `reconcile_calendar_pool_projections` management command that recomputes the projection and reports differences, with `--dry-run` default and `--fix`.

Implements: **Guiding Decisions → Roster resolution**, **Slot ↔ pool arity**, **Roster composition**, **Uniqueness under projection**; goals 1 and 2.

Tests:
- **Integration**: `calendar_integration/tests/services/test_slot_pool_projection.py` — attaching a pool makes its calendars bookable in the slot; detaching removes them; a calendar present both inline and in an attached pool survives the pool being detached; a calendar in two attached pools survives one being detached; deleting a slot removes its attachments and projected rows.
- **Integration**: same file — a slot with two required calendars is **not** satisfiable by a single calendar that appears both inline and via a pool. This is the `Count` regression from **Counting under duplicate calendars** and is the reason that fix ships in this phase.
- **Integration**: same file — a group with no pools attached produces byte-identical availability and bookable-slot output to the same fixture before this phase, with no additional queries.
- **Unit**: `calendar_integration/tests/test_migrations_slot_pool.py` — the constraint swap rejects two inline rows for the same `(slot, calendar)` while accepting an inline row and a projected row for that pair.
- **Unit**: management-command test — a deliberately corrupted projection is detected in dry-run and repaired with `--fix`.

**Suggested AI model**: Tier 4. The constraint swap with NULL semantics, the projection invariant, and the correlated-subquery count fix are each a place where a plausible-looking implementation is silently wrong, and they interact.

**Review models**: reviewer Tier 4 — a projection that drifts or a unique constraint that admits duplicates corrupts roster satisfaction, which decides who can be booked. Fixer Tier 3, since fixes in this phase touch the same delicate migration and counting logic rather than surface code.

**Reusable skills**: `add-migration` with the `migration-author` agent for the constraint swap; `add-model` for the through model.

Acceptance: attaching a pool to a slot makes its calendars bookable through that slot and detaching removes exactly the projected rows; a calendar present from two sources survives losing one; a group with no pools produces identical output and query counts to before the phase.

---

### Phase 4 — Manage pools over internal REST

**Goal**: an org admin can create, list, edit, and delete pools from the Web SPA, and attach them to slots when editing a group.

**Feature flag**: none — self-gating. New routes plus one optional write-only field on an existing serializer; omitting `pool_ids` leaves a group update behaving exactly as today.

Changes:
1. [calendar_integration/serializers.py](calendar_integration/serializers.py): `CalendarPoolSerializer` (with `calendar_ids` write / `calendars` read, mirroring `CalendarGroupSlotSerializer`), and `pool_ids` / `pools` on `CalendarGroupSlotSerializer` at [line 2964](calendar_integration/serializers.py#L2964).
2. [calendar_integration/views.py](calendar_integration/views.py): `CalendarPoolViewSet`, delegating writes to `CalendarGroupService` the way `CalendarGroupViewSet` does, and translating the in-use deletion refusal to a 409 naming the referencing groups.
3. [calendar_integration/permissions.py](calendar_integration/permissions.py): `CalendarPoolPermission` — admins full CRUD, members read-only and only pools containing a calendar they own.
4. [calendar_integration/filtersets.py](calendar_integration/filtersets.py): `CalendarPoolFilterSet` with filtering by member calendar id.
5. [calendar_integration/routes.py](calendar_integration/routes.py): register the viewset.
6. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): `create_pool` / `update_pool` / `delete_pool`, audited through the existing `_audit_group_write` pattern, with `delete_pool` raising a dedicated in-use error listing referencing groups.
7. Regenerate `schema.yml` (`make update_schema`).

Implements: **Guiding Decisions → API surfaces** (REST half), **Visibility scoping**, **Pool deletion**.

Tests:
- **Integration**: `calendar_integration/tests/test_views_calendar_pool.py` — full CRUD; deleting a referenced pool returns 409 and names the groups; roster replacement semantics on update; cross-organization isolation.
- **Integration**: same file — a `scoped_member` sees only pools containing a calendar they own and cannot write; a member whose membership is inactive sees none.
- **Integration**: `calendar_integration/tests/test_views_calendar_group.py` — a group update payload **omitting** `pool_ids` leaves attachments untouched, while an empty list detaches all.

**Suggested AI model**: Tier 3. Seven files, but the permission scoping and the omit-versus-empty-list semantics both need judgement rather than pattern-matching.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: an admin can perform full pool CRUD over REST, deleting an attached pool returns 409 naming the referencing groups, a scoped member sees only pools they participate in, and a group update omitting `pool_ids` changes no attachments.

---

### Phase 5 — Expose pools on the public GraphQL API

**Goal**: partner integrations can read and manage pools through the public API, under the same scope-token model as calendar groups.

**Feature flag**: none — self-gating. New queries, new mutations, new type; no existing operation changes shape.

Changes:
1. [calendar_integration/graphql.py](calendar_integration/graphql.py): `CalendarPoolGraphQLType`, with a `calendars` resolver applying the owner scoping used at [line 829](calendar_integration/graphql.py#L829) so a second hop cannot leak a sibling team's roster.
2. [public_api/constants.py](public_api/constants.py): `PublicAPIResources.CALENDAR_POOL`.
3. [public_api/queries.py](public_api/queries.py): `calendarPool` and `calendarPools`, scoped through a new `scoped_calendar_pool_queryset` in [public_api/scoping.py](public_api/scoping.py) built on `CalendarPoolQuerySet.only_member_of`, fail-closed for missing or inactive memberships exactly as `scoped_calendar_group_queryset` is at [scoping.py:122-152](public_api/scoping.py#L122-L152).
4. [public_api/mutations.py](public_api/mutations.py): create / update / delete pool mutations.
5. [public_api/permissions.py](public_api/permissions.py): every new field name added to `FIELD_TO_RESOURCE_MAPPING`.

Implements: **Guiding Decisions → API surfaces** (GraphQL half), **Visibility scoping**.

Tests:
- **Integration**: `public_api/tests/test_calendar_pool_queries.py` — org-wide, scoped-admin, scoped-member, and revoked-token visibility, including that a scoped member cannot read a non-member pool's roster through `calendarGroup → slots → pools → calendars`.
- **Integration**: `public_api/tests/test_calendar_pool_mutations.py` — create / update / delete, delete-refused-when-attached, cross-organization rejection.
- **Unit**: a test asserting every new field name appears in `FIELD_TO_RESOURCE_MAPPING`, so an unmapped field cannot ship as a scope-check gap.

**Suggested AI model**: Tier 3. Multi-module wiring where the scoping and the resource mapping are both security-relevant and neither is mechanical.

**Review models**: reviewer Tier 4 — this phase decides what a partner token can read, and the second-hop roster leak is exactly the class of bug the existing scoped resolvers were written to prevent. Fixer stays on the project default.

**Reusable skills**: `create-graphql-public-query`.

Acceptance: partners can perform pool CRUD over GraphQL under their token scope, a scoped-member token cannot read a pool it does not participate in through any path including nested traversal, and every new field is present in the resource mapping.

---

### Phase 6 — Stale-selection sweep query

**Goal**: ops can list every event in a group still holding a calendar that has left its slot's roster, so the backlog Phase 1 makes possible can be worked deliberately rather than discovered one edit at a time.

**Feature flag**: none — purely additive read-only surface.

Changes:
1. [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py): `find_stale_selections(group_id, window_start=None, window_end=None)` returning `StaleSelection` triples, computed as selections with no matching `CalendarGroupSlotMembership` row, in one query.
2. [calendar_integration/views.py](calendar_integration/views.py) and [calendar_integration/routes.py](calendar_integration/routes.py): a read-only REST collection, gated by the group-view permission.
3. [public_api/queries.py](public_api/queries.py) and [public_api/permissions.py](public_api/permissions.py): the GraphQL equivalent plus its resource mapping entry.

Implements: the standalone-query half of **Guiding Decisions → Staleness surfaces**; goal 4.

Tests:
- **Integration**: `calendar_integration/tests/test_stale_selection_query.py` — returns exactly the events holding departed calendars, excludes fully-rostered events, honours the date window, and returns nothing for a group whose rosters never changed.
- **Integration**: same file — a query-count assertion proving the result set size does not drive query count.
- **Integration**: scoping — a scoped member sees stale selections only for groups they participate in.

**Suggested AI model**: Tier 2. One service method plus two thin read-only surfaces, all against patterns established by Phases 4 and 5.

**Reusable skills**: `create-rest-endpoint`, `create-graphql-public-query`.

Acceptance: the query returns exactly the `(event, slot, calendar)` triples whose calendar is no longer rostered for that slot, on both REST and GraphQL, with query count independent of result size.

## 6. Risk & Rollout Notes

**The constraint swap in Phase 3 is the highest-risk migration.** Replacing `calendargroupslotmembership_unique_slot_calendar` with a three-column constraint takes an `ACCESS EXCLUSIVE` lock on the membership table while the new index builds. The table is small — one row per calendar per slot per organization — so the lock window is short, but the migration should still add the new constraint before dropping the old one so the table is never briefly unconstrained. If the table has grown beyond expectation in production, build the new index `CONCURRENTLY` outside the transaction and attach it, which the `migration-author` agent and `add-migration` skill handle. The NULL-handling detail is the part most likely to be silently wrong: a plain three-column unique constraint accepts duplicate inline rows, because Postgres treats NULLs as distinct.

**Projection drift is the accepted cost of the chosen resolution strategy.** The mitigations are that every roster-changing write goes through one reconcile entry point inside `transaction.atomic()`, and that Phase 3 ships a management command that recomputes the projection and reports or repairs differences. Run it once after each of Phases 3, 4 and 5 reaches production, in dry-run, and treat any reported difference as a bug in the reconcile path rather than repairing and moving on.

**Phase 1 is the only client-visible behavior change and needs communication before it ships.** An operation that returns a rejection today will start succeeding. Generate the handoff with `handoff-to-client` as part of the phase and send it to the Web SPA and partner-integration owners before merge, not after. There is no flag, so the rollback path is a code revert — which is safe precisely because the phase only removes a rejection and a deletion. Nothing written while the phase is live becomes invalid if it is reverted; the worst case is that removals which succeeded under the new behavior would be rejected again afterwards.

**There is a window between Phase 1 and Phase 2 where stale selections can exist without any way to see them.** Keeping the two phases adjacent in the merge order closes it. If Phase 2 slips, that is a reason to hold Phase 1, not to ship Phase 1 alone.

**Scoped rows now outlive roster membership, which changes what the billing counters see.** `AvailableTime` rows are metered, and `AvailableTimeQuerySet.only_user_authored` feeds that counter. Under the old behavior, removing a calendar from a slot deleted its group-scoped windows and the organization's metered count dropped. Under the new behavior the rows survive, so counts that used to fall no longer do. This is correct — the configuration genuinely still exists — but it means an organization near its limit will not free capacity by editing rosters. Phase 1 should assert the counter's new behavior explicitly rather than leave it to be discovered on an invoice.

**No backfill and no data migration of existing rosters.** Every existing slot keeps its inline membership rows with `source_pool = NULL`, which is exactly today's state. Pools are opt-in per slot.

**The two versioned Postgres quota functions need no change.** `calculate_calendar_group_quota_period_counts` and `get_calendar_group_quota_period_counts_json` take row ids as input, and quota rules remain keyed on `(group_slot, calendar)` regardless of how the calendar reached the slot. Phase 3 should verify this rather than assume it: if either function selects roster rows itself, it needs a version bump and Phase 3 grows accordingly.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should attaching a pool to a slot whose inline roster already contains some of that pool's calendars keep the inline rows, or absorb them into the pool? | **Keep both.** The projection is designed for it, and absorbing would silently destroy the inline intent — the user gets the same bookable roster either way, and detaching later behaves the way they'd expect. | Product |
| When a pool is renamed, should anything be audited beyond the pool itself — for instance a note on each referencing group? | **Audit the pool only.** The referencing groups did not change. If ops needs the reverse view, the stale-selection query and the pool's `group_slots` relation already provide it. | Eng |
| Is there a practical ceiling on pool size, or on the number of pools attached to one slot? | **No hard limit in v1.** Roster reads are indexed and the projection is bounded by pools × calendars, which stays small at realistic organization sizes. Revisit if a tenant attaches more than a handful of large pools to one slot. | Eng |
| Should `scoped_member` tokens be able to see the *names* of pools attached to a slot they participate in, even when they own no calendar in those pools? | **No — fail closed**, matching the visibility rule chosen in Step 0. Listed because it is the most likely thing a client asks for once the UI lands. | Product |

## 8. Touch List

**Phase 0**
- Edited: [calendar_integration/models.py](calendar_integration/models.py), [calendar_integration/managers.py](calendar_integration/managers.py), [calendar_integration/querysets.py](calendar_integration/querysets.py), [calendar_integration/admin.py](calendar_integration/admin.py), [calendar_integration/factories.py](calendar_integration/factories.py), [calendar_integration/virtual_models.py](calendar_integration/virtual_models.py)
- Created: `@calendar_integration/migrations/` (create `CalendarPool`, `CalendarPoolMembership`), `@calendar_integration/tests/test_calendar_pool_querysets.py`

**Phase 1**
- Edited: [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py), [calendar_integration/exceptions.py](calendar_integration/exceptions.py), [calendar_integration/models.py](calendar_integration/models.py) (quota-rule docstring)
- Created: `@calendar_integration/tests/services/test_calendar_group_service_lenient_removal.py`, `@.vinta-ai-workflows/client-handoffs/` (generated)
- Edited (test contract changes): existing group-service and group-scoped-availability suites

**Phase 2**
- Edited: [calendar_integration/graphql.py](calendar_integration/graphql.py), [calendar_integration/serializers.py](calendar_integration/serializers.py), [calendar_integration/virtual_models.py](calendar_integration/virtual_models.py)
- Created: `@calendar_integration/tests/test_stale_selection_flag.py`

**Phase 3**
- Edited: [calendar_integration/models.py](calendar_integration/models.py), [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py), [calendar_integration/services/dataclasses.py](calendar_integration/services/dataclasses.py), [calendar_integration/querysets.py](calendar_integration/querysets.py), [calendar_integration/virtual_models.py](calendar_integration/virtual_models.py)
- Created: `@calendar_integration/migrations/` (through table, `source_pool` column, constraint swap), `@calendar_integration/management/commands/reconcile_calendar_pool_projections.py`, `@calendar_integration/tests/services/test_slot_pool_projection.py`, `@calendar_integration/tests/test_migrations_slot_pool.py`

**Phase 4**
- Edited: [calendar_integration/serializers.py](calendar_integration/serializers.py), [calendar_integration/views.py](calendar_integration/views.py), [calendar_integration/permissions.py](calendar_integration/permissions.py), [calendar_integration/filtersets.py](calendar_integration/filtersets.py), [calendar_integration/routes.py](calendar_integration/routes.py), [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py), `schema.yml` (regenerated)
- Created: `@calendar_integration/tests/test_views_calendar_pool.py`

**Phase 5**
- Edited: [calendar_integration/graphql.py](calendar_integration/graphql.py), [public_api/constants.py](public_api/constants.py), [public_api/queries.py](public_api/queries.py), [public_api/mutations.py](public_api/mutations.py), [public_api/permissions.py](public_api/permissions.py), [public_api/scoping.py](public_api/scoping.py)
- Created: `@public_api/tests/test_calendar_pool_queries.py`, `@public_api/tests/test_calendar_pool_mutations.py`

**Phase 6**
- Edited: [calendar_integration/services/calendar_group_service.py](calendar_integration/services/calendar_group_service.py), [calendar_integration/views.py](calendar_integration/views.py), [calendar_integration/routes.py](calendar_integration/routes.py), [public_api/queries.py](public_api/queries.py), [public_api/permissions.py](public_api/permissions.py)
- Created: `@calendar_integration/tests/test_stale_selection_query.py`
