# Implementation Tracking — calendar-events-user-filter

- **Plan**: `ai-plans/2026-06-18-CALENDAR_EVENTS_USER_FILTER_IMPLEMENTATION_PLAN.md`
- **Feature**: optional `userId` argument on the public_api `calendarEvents` query.
- **Started**: 2026-06-18
- **Last updated**: 2026-06-19

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `commit_strategy_resolved`: modular-commits (single plan branch, one PR)
- `worktree_path`: `.claude/worktrees/plan-calendar-events-user-filter`
- `worktree_branch`: `plan/calendar-events-user-filter`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-calendar-events-user-filter.yaml`
- DB isolation: dedicated `vinta_schedule_api_wt_cal_events` in shared Postgres; compose project pinned to `vinta-schedule`.

## Plan branch
`plan/calendar-events-user-filter` (rebased onto `origin/main` @ c33708f after the user's main updates).

## Completed phases
### Phase 1 — Multi-calendar expansion service method ✅
- Model used: claude-sonnet-4-6 (plan Tier 3).
- Commit: `feat(calendar): add multi-calendar event expansion service method` (amended after review).
- Added `CalendarEventService.get_calendar_events_expanded_for_calendars(calendar_ids, start, end, optimize_queryset=None)` + thin facade on `CalendarService`.
- Org-guarded (`organization_id` + `calendar_fk__in`); empty/None ids → `[]` with no query.
- Dedup: bundle-representation dropped, bundle-primary once per id, real rows by id, generated occurrences (pk=None) kept unconditionally.
- Review: 3 layers. Reviewer caught a dedup-collision BLOCKER (distinct recurring series sharing a start_time keyed on `(None, start_time)`); fixer reworked dedup + added regression tests + replaced a bare `raise` with `PermissionDenied`.
- Tests: `calendar_integration/tests/services/test_get_calendar_events_expanded_for_calendars.py` (11 tests). Outer gate green (full suite passes apart from a pre-existing unrelated `test_org_resolution` xdist flake that passes in isolation).

## Current phase
Phase 2 — Wire `userId` into the `calendarEvents` query (pending).

## Remaining phases
- Phase 2 — Wire `userId` into the public_api `calendarEvents` query + integration tests.

## Deferred phases
- None (no cross-repo, no feature-flag-removal phase in this plan).
