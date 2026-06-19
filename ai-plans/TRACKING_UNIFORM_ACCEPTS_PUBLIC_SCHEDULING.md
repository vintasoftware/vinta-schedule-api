# Tracking — UNIFORM_ACCEPTS_PUBLIC_SCHEDULING

- **Feature**: Uniform `accepts_public_scheduling` / `is_private` across Calendar, CalendarGroup & bundles
- **Plan**: `ai-plans/2026-06-18-UNIFORM_ACCEPTS_PUBLIC_SCHEDULING_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-18
- **Last updated**: 2026-06-19

## Run options
- `commit_strategy_resolved`: modular-commits (single branch `plan/uniform-accepts-public-scheduling`, one PR)
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
  - `worktree_path`: `.claude/worktrees/plan-uniform-accepts-public-scheduling`
  - `worktree_branch`: `plan/uniform-accepts-public-scheduling` (base `main`)
  - `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-uniform-accepts-public-scheduling.yaml`
- `plan_branch`: `plan/uniform-accepts-public-scheduling`

## Notes
- No feature flag (project has none). No flag-removal phase.
- No cross-repo phases. All 7 phases executable in-repo.
- Branch rebased onto `main` (`a5e470d`) after Phase 1 at user request ("issues fixed in main").
  Removed stray untracked `public_api/migrations/0008_merge_*` + `0009_alter_resourceaccess_*`
  (auto-generated cruft referencing a node main deleted) to restore a clean linear graph.
- PR not yet opened: `gh` / `yq` missing on host → `open-pr.sh` can't run. prs-context written
  `status: pending` at `.vinta-ai-workflows/prs-context/uniform-accepts-public-scheduling/plan.md`.

## Completed phases

### Phase 1 — Add `accepts_public_scheduling` to `CalendarGroup` ✅
- **Model used**: `claude-haiku-4-5` (plan tier: Tier 1). Agent: `migration-author`.
- **Commit**: `feat(calendar): add accepts_public_scheduling to CalendarGroup`
- **Migration**: `calendar_integration/migrations/0021_calendargroup_accepts_public_scheduling.py` (single `AddField`, `default=False`, no backfill).
- **Files**: `calendar_integration/models.py`, the migration, `calendar_integration/tests/test_calendar_group_models.py` (3 tests).
- **Gate**: `check --deploy` clean; `makemigrations --check` clean; full suite **2450 passed**.
- **Review**: Layer 3 reviewer — no BLOCKER/SHOULD-FIX; 2 NITs (both no-change-required).
- **Summary**: Adds the canonical privacy knob to `CalendarGroup` mirroring `Calendar.accepts_public_scheduling`. Default False (private), no backfill — secure-by-default. Column unread this phase (purely additive); read/write surfaces land in later phases.

## Current phase
- Phase 2 — Expose `is_private` (read) on the three GraphQL types (next).

## Remaining phases
- Phase 2 — Expose `is_private` (read) on Calendar/Group/Bundle GraphQL types.
- Phase 3 — Accept `is_private` on CalendarGroup create/update inputs.
- Phase 4 — Accept `is_private` on bundle create/update inputs.
- Phase 5 — Accept `is_private` on resource-calendar input.
- Phase 6 — New plain-Calendar create/update mutation with `is_private`.
- Phase 7 — Gate codeless public group booking on `accepts_public_scheduling` (behavioral; breaking-change coordination).

## Deferred phases
- None.
