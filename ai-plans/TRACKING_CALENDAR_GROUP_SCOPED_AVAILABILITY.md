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

## Current phase

Phase 1a — Group-scoped availability windows: writes

## Remaining phases

1a, 1b, 1c, 1d, 2a, 2b, 2c, 3a, 3b, 3c, 4

## Deferred phases

_(none — no cross-repo phases, no flag-removal phase)_
