# Tracking — Calendar Group-Scoped Availability

- **Plan**: `ai-plans/2026-08-05-CALENDAR_GROUP_SCOPED_AVAILABILITY_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-08-05
- **Last updated**: 2026-08-05
- **Feature flag**: none (self-gating via fall-through default; metering change in Phase 2c is not self-gating)

## Run options

- `pause_between_phases`: false
- `generate_inline_comments`: false
- `full_test_suite`: false (scoped)
- `use_worktree`: true
- `commit_strategy_resolved`: stacked-branches
- `worktree_path`: `.claude/worktrees/plan-calendar-group-scoped-availability`
- `worktree_branch`: `plan-calendar-group-scoped-availability`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-calendar-group-scoped-availability.yaml`
- `sandbox_tier`: enforced

## Agent models

- implementer: per-phase (plan-owned)
- reviewer: T3→sonnet default; T4→opus on Phases 0, 1b, 3b
- fixer: T2→haiku
- worktree_prep: T1→haiku (done)
- integrate: T1→haiku

## Branch topology (stacked)

Phase 0 branches off `plan-calendar-group-scoped-availability`; each later phase stacks on the previous.

## Completed phases

### Phase 0 — Group-slot scoping on the availability tables ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-0` (base: `feat/calendar-group-scoped-availability`) — **PR [#218](https://github.com/vintasoftware/vinta-schedule-api/pull/218)**
- **Implementer model**: sonnet (plan Tier 3) — agent type `migration-author`
- **Reviewer model**: opus (plan Tier-4 override) — no BLOCKERs; 1 doc SHOULD-FIX (recorded here), 2 NITs left as intended behavior
- **Summary**: Added nullable `group_slot` `OrganizationForeignKey` (composite `group_slot`/`group_slot_fk`) on `AvailableTime` and `BlockedTime`, `on_delete=CASCADE`, with partial composite indexes on non-null rows. Default managers (`objects`) now exclude group-scoped rows via `base_rows_only()`; explicit `for_group_slot(id)` / `unscoped()` accessors expose the rest. `only_user_authored` stays scope-agnostic at the queryset level. Admin uses `unscoped()`. Lock-aware migration `0042`: metadata-only `ADD COLUMN`, FK `NOT VALID` + separate `VALIDATE`, `AddIndexConcurrently`, `DEFERRABLE INITIALLY DEFERRED` constraints (load-bearing for cascade correctness on nullable-CASCADE relations). Zero edits to existing call sites; full repo suite (4631) passed.
- **SQL-function verification (Open Question 1 — RESOLVED)**: The occurrence functions (`calculate_recurring_available_times`, `calculate_recurring_blocked_times`, their `_with_bulk_modifications` and `get_*_occurrences_json` variants) all take a row id as input and select no rows themselves (e.g. `WHERE id = p_available_time_id`). Scoping is applied caller-side. **No SQL function version bumps were needed** — the phase did not grow.
- **Carry-forward note for Phases 1b / 2a / 3b**: `RecurringMixin._get_occurrences_in_range` re-fetches `self` via the *default* (base-rows-only) manager, so calling `get_occurrences_in_range()` on a group-scoped instance would silently return nothing once such rows exist. Unreachable today (nothing writes `group_slot` yet). Group-scoped occurrence expansion must route through `for_group_slot`/`unscoped`, not that instance method as written.

### Phase 0b — Organization week-start setting ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-0b` (base: phase-0) — **PR [#219](https://github.com/vintasoftware/vinta-schedule-api/pull/219)**
- **Implementer model**: haiku (plan Tier 1) — agent type `migration-author`
- **Reviewer model**: sonnet — no BLOCKERs; 3 SHOULD-FIX + 1 NIT, all fixed by a haiku fixer
- **Summary**: Added `Organization.week_start` (`TextChoices` Monday/Sunday, `default` + `db_default` Monday) mirroring `external_event_update_policy`. Migration `0018` adds the column with a `db_default` (existing rows backfill to Monday, no data migration). Exposed in Django admin (editable via `OrganizationAdminForm`, in `list_filter`), and on the organization serializer; gated admin-only via `IsOrganizationAdmin` on update/partial_update. Wired `week_start` through `create_organization` service so it's settable at creation, matching the sibling policy field. Nothing reads the field yet.
- **Fixes applied**: made `week_start` admin-editable (was erroneously read-only); wired it through the create path (was silently dropped at create); added a raw-SQL `db_default` test proving pre-migration rows read Monday.

### Phase 1a — Group-scoped availability windows: writes ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-1a` (base: phase-0b) — **PR [#220](https://github.com/vintasoftware/vinta-schedule-api/pull/220)**
- **Implementer model**: sonnet (plan Tier 3) — agent type `implementer`
- **Reviewer model**: sonnet — 1 BLOCKER + 2 SHOULD-FIX + 1 reopened audit gap, all fixed (2 haiku fixer rounds)
- **Summary**: Service-layer create/update/delete for group-scoped availability windows on `CalendarGroupService`. Writes/reads via `AvailableTime.objects.for_group_slot(...)`/`.unscoped()`. New `_group_scoped_available_times_expanded` (annotates occurrences before `get_occurrences_in_range` to dodge the Phase-0 base-manager re-fetch). Non-disclosure permissions via identical `CalendarGroupSlotConfigNotFoundError` (stranger / other-calendar-owner / missing-membership all indistinguishable). Audit on all ops with before/after diff. Orphaned-booking detection on narrowing update AND first-window create (returns them in `GroupScopedAvailabilityWriteResult`, mutates nothing). New `CalendarPermissionService.can_manage_group_scoped_calendar_config`. No migration.
- **BLOCKER fixed**: `_reconcile_slot` now deletes (and audits) group-scoped windows when a calendar is removed from a slot — the slot-FK cascade fires only on slot deletion, not membership removal (spec edge case + plan's required test).

**CARRY-FORWARD (occurrence-expansion trap) — RESOLVED in Phase 1b:**
`RecurringMixin._get_occurrences_in_range` (calendar_integration/models.py ~L835-854) now guards the inner exception-instance lookup: when `getattr(self, "group_slot_fk_id", None) is not None` it uses `self.__class__._base_manager` (unfiltered), else the default manager. So a recurrence exception on a group-scoped master is found, not silently missed. All other recurring models are unchanged (guarded on the group-scoped case only). Covered by `test_group_scoped_recurring_exception_is_honored_when_master_is_group_scoped`. No longer an open risk for Phases 2a/3b.

### Phase 1b — Windows in discovery and booking validation ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-1b` (base: phase-1a) — **PR [#221](https://github.com/vintasoftware/vinta-schedule-api/pull/221)**
- **Implementer model**: sonnet (plan Tier 3) — agent type `implementer`
- **Reviewer model**: opus (plan Tier-4 override) — no BLOCKERs; 2 SHOULD-FIX + 2 NIT fixed (haiku fixer)
- **Summary**: Group-scoped windows intersected into slot-engine discovery + booking/reschedule validation. **Zero-change early-out** via an `Exists()` annotation folded into the existing membership query (no new round trip; `find_bookable_slots` 6→6, `check_group_availability` 5→5 queries; byte-identical output — asserted). Intersect-only via `base_free` short-circuit. New `slot_engine` batched span fetch (`fetch_group_scoped_available_spans`, recurrence-aware), consumed by discovery and by `_assert_calendars_within_group_scoped_windows` (wired into create + reschedule; reschedule extended to all selected calendars). New `GroupScopedRuleType` + `CalendarGroupScopedRuleViolationError` (names calendar + rule type, no configured-value leak). `bookable_slots_service.py` untouched. Resolved the recurrence-exception trap (see above). No migration.

### Phase 1c — Windows on the internal REST surface ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-1c` (base: phase-1b) — **PR [#222](https://github.com/vintasoftware/vinta-schedule-api/pull/222)**
- **Implementer model**: sonnet (escalated — initial haiku attempt DISCARDED for committing failing tests, skipping required tests, and removing the required route-level gating; it also wrongly amended the tracking commit) — agent type `implementer`
- **Reviewer model**: sonnet — no BLOCKERs; 2 SHOULD-FIX + 1 NIT fixed (sonnet fixer)
- **Summary**: Nested REST viewset `.../calendar-groups/{group_id}/slots/{slot_id}/availability-windows/`, thin, delegating to Phase 1a `CalendarGroupService`. `GroupScopedAvailabilityWindowPermission` for route-level group-visibility gating; **non-disclosure byte-identical** across stranger / cross-org / other-calendar-owner / missing-or-mismatched slot (all `Http404 {"detail":"Not found."}`, asserted via `data ==`). Create/update responses wrap `{window, orphaned_bookings}`; list/retrieve return the window. Tri-state recurrence editing (`PATCH null` clears, omit leaves, string sets) via `_UNCHANGED` sentinel. Narrow virtual model + bounded-query test. Schema purely additive. No migration.
- **Process note**: reinforced hard rules on the retry (no amend, no commit with red tests, gating is required not optional).

### Phase 1d — Windows on the public API ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-1d` (base: phase-1c) — **PR [#223](https://github.com/vintasoftware/vinta-schedule-api/pull/223)**
- **Implementer model**: sonnet (plan Tier 3) — agent type `implementer`
- **Reviewer model**: sonnet — 1 security BLOCKER (IDOR) + 2 SHOULD-FIX + NITs, all fixed (sonnet fixer)
- **Summary**: Public GraphQL `group_scoped_availability_windows` query + `batch_upsert_group_scoped_availability_windows` mutation. Batch semantics mirror the existing availability batch write exactly (all-or-nothing via own `@transaction.atomic()` + `SELECT FOR UPDATE`, NOT relying on prod-only `ATOMIC_REQUESTS`; over-limit rejects whole with byte-identical body; content-match idempotent replay for create, explicit `windowId` for update/delete). Fixed the entitlement counter `_count_availability_windows` to `unscoped().only_user_authored()` so group-scoped windows meter (composes correctly — still excludes non-user-authored rows). Public-API token auth (`OrganizationResourceAccess` + `assert_calendar_in_owner_scope`). Existing availability ops frozen (asserted). No migration.
- **Security BLOCKER fixed (IDOR)**: update/delete now cross-check the resolved window's `calendar_fk_id == op.calendar_id` (was authorizing only by the op's own calendar, letting an owner-scoped token modify another calendar's window in the same slot via a foreign `windowId`). **Concurrency fix**: billing-root lock now taken before the idempotency content-match (concurrent UC-5 retries no longer double-create).

### Phase 2a — Group-scoped blocked time: writes and enforcement ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-2a` (base: phase-1d) — **PR [#224](https://github.com/vintasoftware/vinta-schedule-api/pull/224)**
- **Implementer model**: sonnet (plan Tier 2, stepped up per plan's "invasive resolution-order change" clause) — agent type `implementer`
- **Reviewer model**: sonnet — no BLOCKERs; 2 test-coverage SHOULD-FIX + NITs fixed (haiku fixer)
- **Summary**: Group-scoped blocked-time writes (create/update/delete on `CalendarGroupService`, `BlockedTime` rows with `group_slot`), mirroring Phase 1a (audit / permissions / non-disclosure / orphan detection). Slot-engine enforcement: **block beats window** on all three paths (block excludes before window-coverage runs); rejection `GroupScopedRuleType.INSIDE_BLOCK`. Every block create/update runs orphan detection (each block independently subtracts time). Zero-change early-out (second `Exists()` folded in; 6/5 query counts unchanged). `_reconcile_slot` extended via shared `_delete_group_scoped_rows_for_removed_calendars` (deletes+audits windows AND blocks on membership removal). No migration.

**REFACTOR NOTE for Phase 3a**: the get / audit / create / update / delete quintet is duplicated ~1:1 between windows (Phase 1a) and blocks (Phase 2a). Phase 3a adds quota — before tripling the pattern, consider a generic model-parameterized helper (`_get_group_scoped_row(model, id)` / `_audit_group_scoped_write(model_label, ...)`). Only if it doesn't over-couple; the plan phases by concept deliberately.

### Phase 2b — Blocks on the REST and public surfaces ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-2b` (base: phase-2a) — **PR [#225](https://github.com/vintasoftware/vinta-schedule-api/pull/225)**
- **Implementer model**: sonnet (plan Tier 2, stepped up given the 1c haiku failure) — agent type `implementer`
- **Reviewer model**: sonnet — 2 BLOCKERs (restriction bypass + test-registry gap) + 1 SHOULD-FIX, all fixed (sonnet fixer)
- **Summary**: Full REST + public API parity for blocks, mirroring windows (Phase 1c/1d). Route `.../slots/{slot_id}/blocked-times/`; non-disclosure identical `Http404`; IDOR calendar-match on batch update/delete; tri-state rrule; bounded queries; content-match idempotency (key includes `reason`). Blocks unmetered (2c does that) but `check_not_restricted`-guarded. Schema additive; no migration.
- **Security BLOCKER fixed (restriction bypass)**: all six single-write group-scoped methods (window + block create/update/delete) now call `_check_not_restricted()` — previously a RESTRICTED org could write via the REST viewsets. New `GROUP_SCOPED_WRITE_PROBES` registry + test class in `test_restricted_enforcement.py` so CI catches it.

**FOLLOW-UP NOTE (metering enforcement on single-write creates)**: single-write REST creates don't call `check_limit`, so the AVAILABILITY_WINDOWS limit is enforced only on the batch (public API) path. Appears consistent with existing base-availability behavior. Phase 2c owns the metering *counter* (what counts); whether single-write REST create should also *enforce* the limit is a separate consistency question — flag if Phase 2c's work makes it trivial to close.

### Phase 2c — Meter all blocked time ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-2c` (base: phase-2b) — **PR [#226](https://github.com/vintasoftware/vinta-schedule-api/pull/226)**
- **Implementer model**: sonnet (plan Tier 2, run on sonnet for billing sensitivity) — agent type `implementer`
- **Reviewer model**: sonnet — CLEAN (no BLOCKER / SHOULD-FIX / NIT)
- **Summary**: `_count_availability_windows` now sums user-authored availability windows + blocked time (base + group-scoped, both `unscoped()`). Added `BlockedTimeQuerySet.only_user_authored` (faithful translation — `BlockedTime` shares `RecurringMixin` and the same generic recurrence machinery, so `exception_for`/`bulk_modification_parent`/`is_recurring_exception` have identical semantics; exceptions/splits don't inflate). Over-limit uses the existing `OverLimitError` path; the limit-warning notification picks up blocked-time usage automatically via `get_current_usage`. Open Question 2 resolved (no field gap). No migration.

### Phase 3a — Quota model and period-counting function ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-3a` (base: phase-2c) — **PR [#227](https://github.com/vintasoftware/vinta-schedule-api/pull/227)**
- **Implementer model**: sonnet (plan Tier 3) — agent type `migration-author`
- **Reviewer model**: sonnet — 1 BLOCKER (quota cap-bypass via per-event timezone bucketing) fixed (sonnet fixer)
- **Summary**: `CalendarGroupSlotQuotaRule` model (slot, calendar, period `QuotaPeriod` day/week/month, cap; unique per (calendar,slot,period); cascade on slot deletion; membership-removal cleanup wired into `_reconcile_slot`). Versioned Postgres `calculate_calendar_group_quota_period_counts` + JSON wrapper counting live bookings made through the group (via `CalendarEventGroupSelection`; cancelled = deleted event; reschedule re-buckets on read). Migrations `0043`/`0044` reverse cleanly. `GetCalendarGroupQuotaPeriodCountsJSON` wrapper (`Value()`-wraps string args).
- **BLOCKER fixed (quota bypass)**: bucketing now uses a single consistent UTC frame (was per-event booker-supplied `ce.timezone`, which let a per-period cap be exceeded by varying the booking timezone). Matches the plan's timezone-less function signature. Local-timezone quota alignment deferred (no canonical calendar tz field in schema). Regression test added.

**CARRY-FORWARD for Phase 3b**: quota periods are measured in **UTC** (documented v1 simplification). Phase 3b's discovery/booking lookup MUST bucket the candidate booking's time in the SAME UTC frame the counting function uses, so the candidate's period matches the counted period. Do NOT reintroduce per-event/local timezone bucketing on the candidate side.

### Phase 3b — Quota in discovery and booking validation ✅

- **Branch**: `plan/calendar-group-scoped-availability/phase-3b` (base: phase-3a) — **PR [#228](https://github.com/vintasoftware/vinta-schedule-api/pull/228)**
- **Implementer model**: sonnet (plan Tier 3) — agent type `implementer`
- **Reviewer model**: opus (plan Tier-4 override) — no BLOCKERs; 1 SHOULD-FIX (reschedule self-exclusion) + coverage gaps + NITs fixed (sonnet fixer)
- **Summary**: Quota wired into discovery + booking/reschedule. **One counting query per discovery, independent of candidate count** (batched by `(slot,period)`; asserted 1 for few vs ~50× candidates, 0 unconfigured; `check_group_availability` 1 across 200 ranges). Third `Exists()` folded into the membership query (self-gating; 6/5 counts hold). `quota_period_start_utc` mirrors the SQL bucketing (pin test prevents drift). `quota_covering_range` prevents same-period undercount. Quota checked LAST; all rules must pass. `QUOTA_CONSUMED` error (cap not leaked). Cancel releases quota on read. No migration.
- **SHOULD-FIX fixed (reschedule)**: the event being rescheduled is self-excluded from its own period's quota count, so a `cap=1` calendar can reschedule same-day (was universally rejected). Reschedule across a boundary into a full period still correctly rejected.

## Current phase

Phase 3c — Quota rules on the REST and public surfaces

## Remaining phases

3c, 4

## Deferred phases

_(none — no cross-repo phases, no flag-removal phase)_
