# Tracking — Update Resource Calendar

- **Feature**: Update Resource Calendar (dedicated `updateResourceCalendar` mutation + service method)
- **Plan**: `ai-plans/2026-06-22-UPDATE_RESOURCE_CALENDAR_IMPLEMENTATION_PLAN.md`
- **Plan id**: `2026-06-22-UPDATE_RESOURCE_CALENDAR`
- **Started**: 2026-06-22
- **Last updated**: 2026-06-22
- **Feature flag**: none (purely additive surface)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `commit_strategy_resolved`: modular-commits
- `plan_branch`: `plan/update-resource-calendar`
- `worktree_path`: `/Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-update-resource-calendar`
- `worktree_branch`: `plan/update-resource-calendar`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-update-resource-calendar.yaml`

## Completed Phases
### Phase 1 — `update_resource_calendar` service method + guard ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop clean).
- **Model**: claude-haiku-4-5 (plan tier: Tier 2).
- **Commits** (on `plan/update-resource-calendar`):
  - `d177750` feat(calendar): add update_resource_calendar service method with INTERNAL+RESOURCE guard
  - `ce7b191` test(calendar): cover update_resource_calendar audit branch + narrow capacity type hint
- **Summary**: Added `CalendarService.update_resource_calendar(calendar_id, *, name, description, capacity, manage_available_windows, accepts_public_scheduling, visibility)` in `calendar_integration/services/calendar_service.py`, mirroring `update_calendar` / `disable_resource_calendar`. Module-level `_UNCHANGED` sentinel gives capacity three states: omit (untouched), `None` (clear to unlimited), int (set). Guard runs after the org-scoped `filter_by_organization(...).get()` lookup — synced (`provider != INTERNAL`) and non-RESOURCE calendars raise `ValueError`; cross-org surfaces as `Calendar.DoesNotExist` (no existence leak). Audit reuses `_audit_calendar_write(AuditAction.UPDATE, ..., diff=compute_diff(before, after))`, only when a field changed. 16 unit tests (incl. 3 capacity states, both guards, cross-org, and the audit branch).
- **Review note**: reviewer findings — type-hint widening (fixed → `int | None  # type: ignore`) and audit-branch tests (added). Skipped: audit-gating change + bare-`raise`/`visibility: str` NITs (match existing precedent).
- **Incident**: the first fixer attempt ran in the main checkout and committed onto local `main` (`8c04af1`); detected via Layer 1, reverted `main` to `3b94cd8`, re-dispatched the fixer with navigation guards. Worktree branch never affected.

## Current Phase
- **Phase 2** — `updateResourceCalendar` Public GraphQL mutation (Tier 3). Next.

## Remaining Phases
_None after Phase 2._

## Deferred Phases
_None (no cross-repo, no flag-removal phases)._
