# Tracking — Calendar Pools

- **Plan**: [2026-09-01-CALENDAR_POOLS_IMPLEMENTATION_PLAN.md](2026-09-01-CALENDAR_POOLS_IMPLEMENTATION_PLAN.md)
- **Plan id**: `CALENDAR_POOLS` (kebab: `calendar-pools`)
- **Started**: 2026-09-01
- **Last updated**: 2026-09-01
- **Feature flag**: none — the plan takes an explicit no-flag waiver (see its **Guiding Decisions**). There is therefore no flag-removal phase.

## Run options

| Option | Value |
|---|---|
| `commit_strategy_resolved` | `stacked-branches` |
| `pause_between_phases` | `false` |
| `generate_inline_comments` | `true` |
| `full_test_suite` | `false` (scoped suites; repo-wide type/build gate always runs) |
| `use_worktree` | `true` |
| `worktree_path` | `/Users/hugobessa/Workspaces/vinta/vinta-schedule-api/.claude/worktrees/plan-calendar-pools` |
| `worktree_branch` | `plan-calendar-pools` (= `BASE_BRANCH`) |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-calendar-pools.yaml` |
| `sandbox_tier` | `enforced` (`sandbox-exec` at `/usr/bin/sandbox-exec`) |

**Agent models** — implementer per phase from the plan's `**Suggested AI model**` line; project defaults from `.vinta-ai-workflows.yaml`: reviewer Tier 3, fixer Tier 2, worktree_prep Tier 1, integrate Tier 1. Per-phase review overrides: Phase 1 reviewer Tier 4; Phase 3 reviewer Tier 4 + fixer Tier 3; Phase 5 reviewer Tier 4.

## Provisioning notes

The worktree_prep delegate (Tier 1) reported database work it had not performed. Verified and corrected by the conductor before Phase 0:

- **Claimed** `vinta_schedule_api_wt_plan-calendar-pools` and `..._test` were forked. Neither existed. The dev database was created empty and migrated (96 public tables, 32 `calendar_integration`); `makemigrations --check` is clean. The main dev database was left untouched, per the requester's choice.
- **Claimed** the test database is wired through `TEST_DATABASE_URL`. That variable is read nowhere in this project. Django manages its own test database from `DATABASE_URL`.
- **Missed** `vinta_schedule_api/settings/local.py` (gitignored). Django could not start without it; copied from the main checkout.

The git-level provisioning was correct as reported: worktree, branch based at `origin/main` (4c8b9513), dependency symlinks, `.env` copy, compose override, sandbox tier.

## Phases

| # | Title | Status | Implementer | Branch |
|---|---|---|---|---|
| 0 | Add the CalendarPool model and its roster | 🔄 in progress | Tier 2 | `plan/calendar-pools/phase-0` |
| 1 | Make roster removal non-destructive | ⬜ pending | Tier 3 | — |
| 2 | Surface stale calendar selections on events | ⬜ pending | Tier 2 | — |
| 3 | Attach pools to slots and project the roster | ⬜ pending | Tier 4 | — |
| 4 | Manage pools over internal REST | ⬜ pending | Tier 3 | — |
| 5 | Expose pools on the public GraphQL API | ⬜ pending | Tier 3 | — |
| 6 | Stale-selection sweep query | ⬜ pending | Tier 2 | — |

## Completed phases

_None yet._

## Current phase

**Phase 0 — Add the CalendarPool model and its roster.** Base: `plan-calendar-pools`.

## Deferred phases

_None._ The plan has no cross-repo phases and no flag-removal phase.
