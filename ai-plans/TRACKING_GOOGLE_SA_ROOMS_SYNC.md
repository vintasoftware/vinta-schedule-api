# Tracking — GOOGLE_SA_ROOMS_SYNC

- **Feature**: Google Service Account Rooms Sync Fix
- **Plan**: `ai-plans/2026-06-09-GOOGLE_SA_ROOMS_SYNC_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-13
- **Last updated**: 2026-06-13
- **Feature flag**: none (plan explicitly forbids one — rooms sync has never worked)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `commit_strategy_resolved`: modular-commits
- `plan_branch`: plan/google-sa-rooms-sync
- `worktree_path`: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-google-sa-rooms-sync
- `worktree_branch`: plan/google-sa-rooms-sync
- `worktree_summary`: .vinta-ai-workflows/worktrees/plan-google-sa-rooms-sync.yaml

## Completed phases
_None yet._

## Current phase
Phase 1 — Rename `audience` → `admin_email` (model/API/service). NOT STARTED.

Agent type: migration-author (introduces a RenameField migration). Suggested tier: Tier 1 (haiku).

## Remaining phases
- Phase 1 — Rename `audience` → `admin_email` in model, API, service layer. Tier 1. Skill: add-migration.
- Phase 2 — Replace broken JWT auth with `google.oauth2.service_account.Credentials` + DWD. Tier 3.
- Phase 3 — Use Admin SDK to list Workspace resource calendars. Tier 3.

## Deferred phases
_None — no cross-repo or flag-removal phases in this plan._

## Notes
- Worktree provisioned, smoke-tested clean (lint + Django check + 18 tests pass on isolated test DB).
- Plan doc committed onto the plan branch (was untracked in main).
- Side fix (unrelated to this plan): project sub-agent frontmatter was invalid YAML → PR #63
  `fix(ai-tools): emit YAML-safe agent description frontmatter` on branch
  `chore/fix-agent-frontmatter-yaml`. Must be merged to `main` before the real
  `migration-author`/`implementer`/`reviewer`/`fixer` agents register on restart.
