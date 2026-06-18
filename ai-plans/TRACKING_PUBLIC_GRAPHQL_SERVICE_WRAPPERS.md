# Tracking — Public GraphQL Service Wrappers

- **Feature**: Public GraphQL Service Wrappers
- **Plan**: ai-plans/2026-06-17-PUBLIC_GRAPHQL_SERVICE_WRAPPERS_IMPLEMENTATION_PLAN.md
- **Started**: 2026-06-17
- **Last updated**: 2026-06-17
- **Feature flag**: none (purely additive surface; per-token PublicAPIResources grants gate exposure)

## Run options
- `commit_strategy_resolved`: stacked-branches
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
  - `worktree_path`: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-public-graphql-service-wrappers
  - `worktree_branch`: plan/public-graphql-service-wrappers/wt
  - `worktree_summary`: .vinta-ai-workflows/worktrees/plan-public-graphql-service-wrappers.yaml
- `pr_creation`: agents-create
- branch pattern: `plan/public-graphql-service-wrappers/phase-{id}`

## Gate execution (IMPORTANT for resume)
Tests/lint/build run INSIDE docker compose (per skills updated on main @868627d — "use docker compose for environment consistency"). Host Postgres is NOT used (host ephemeral-port exhaustion). Recipe, from the worktree dir:
```
COMPOSE_PROJECT_NAME=vinta-schedule docker compose run --rm api uv run <cmd>
```
- Reuses the shared `vinta-schedule` stack (db/broker/redis already up); mounts worktree code; DB over docker network.
- `vinta_schedule_api/settings/local.py` is a real COPY in the worktree (NOT a symlink) — a symlink breaks inside the container (`ModuleNotFoundError: settings.local`).
- Branch base rebased onto origin/main @868627d.

## Completed Phases
### Phase 0 — Shared mutation scaffolding ✅
- Branch: `plan/public-graphql-service-wrappers/phase-0` (base: origin/main @868627d)
- Model: claude-haiku-4-5 (plan tier 2). Fixer: haiku. Reviewer: sonnet.
- Files: `public_api/mutations.py`, `public_api/tests/test_mutations.py`.
- Added `CalendarMutationDependencies` + `get_calendar_mutation_dependencies()` (DI getter) and `_get_org_and_init_calendar_service(info)` helper (resolves org from request context, inits `calendar_service` via `initialize_without_provider`). No GraphQL fields/constants/permissions yet — pure scaffolding consumed by later phases.
- Review: no BLOCKERs. SHOULD-FIX applied — reverted unrelated `get_mutation_dependencies` message (scope creep); tightened tests (`pytest.raises(GraphQLError)`, behavioral assertions on `calendar_service.organization`/`user_or_token`, top-level imports).
- Outer gate (docker compose): ruff ✅, format ✅, check --deploy ✅, pytest -n auto = 2004 passed.

### Phases 1a / 1b / 1c — DROPPED (2026-06-18)
Single-calendar event create/reschedule/cancel not exposed via Public API. `CalendarEventService.create_event`/`update_event` raise `PermissionDenied("Events cannot be created through the Public API.")` for `SystemUser` (calendar_event_service.py:246, :447). Owner decided the guard is authoritative. Phase-1a work (createCalendarEvent) was reverted (branch deleted, never pushed). Phase 0 scaffolding retained.

## Current Phase
- Phase 2a — createResourceCalendar (in progress), stacked on phase-0

## Remaining Phases
2a, 2b, 2c, 3a, 3b, 3c, 3d, 3e, 3f, 3g, 4a, 4b, 4c, 4d, 5a (REVISIT — group reschedule routes through update_event guard), 5b (REVISIT)

## Deferred Phases
_(none — no cross-repo, no flag-removal phases in this plan)_
