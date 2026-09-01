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
| 0 | Add the CalendarPool model and its roster | ✅ done | Tier 2 (sonnet) | `plan/calendar-pools/phase-0` |
| 1 | Make roster removal non-destructive | 🔄 next | Tier 3 | — |
| 2 | Surface stale calendar selections on events | ⬜ pending | Tier 2 | — |
| 3 | Attach pools to slots and project the roster | ⬜ pending | Tier 4 | — |
| 4 | Manage pools over internal REST | ⬜ pending | Tier 3 | — |
| 5 | Expose pools on the public GraphQL API | ⬜ pending | Tier 3 | — |
| 6 | Stale-selection sweep query | ⬜ pending | Tier 2 | — |

## Completed phases

### Phase 0 — Add the CalendarPool model and its roster

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-0`, base `plan-calendar-pools`. **Commits**: `d60dce65` (plan + tracking), `a9578078` (feature).
- **Models**: implementer Tier 2 (sonnet), reviewer Tier 3 (sonnet). No fixer spawned — no BLOCKER or SHOULD-FIX.
- **Landed**: `CalendarPool` + `CalendarPoolMembership` in `calendar_integration/models.py`, with manager, queryset (`only_member_of`), admin (roster-count annotation + membership inline), virtual models, function-style factories, and migration `0050_calendarpool_calendarpoolmembership_and_more.py`. 9 files, **606 insertions, 0 deletions** — purely additive, nothing reads the models yet.
- **Migration**: creates `calendar_integration_calendarpool` (unique `(organization, name)`) and `calendar_integration_calendarpoolmembership` (unique `(pool_fk, calendar_fk)`), plus the `calendars` m2m and `(organization, id)` indexes on both. All reversible built-in operations, dependency `0049`.
- **Gate, re-run independently by the conductor** rather than taken from the implementer's report: `ruff check` clean; `makemigrations --check` no changes; `check --deploy` 0 errors (5 pre-existing local-settings warnings); `mypy` success across 761 files; `pytest calendar_integration/tests/ -n auto` **2148 passed**. Every claim matched.
- **Review**: three layers clean. Zero BLOCKER, zero SHOULD-FIX, three NITs.

**Accepted NIT, deliberately not fixed.** The two new factory creators lack precise parameter type hints, which AGENTS.md requires of every function. Every neighbouring creator in `calendar_integration/factories.py` has the identical gap and mypy passes clean, so fixing only these two would make the file less internally consistent, and a fixer pass mandates a full outer-gate re-run for four lines. Fold it into any future sweep that types that module. The other two NITs needed no action: a plan-text naming mismatch (plan corrected instead, below) and the `plan/*` branch name not matching AGENTS.md's `feature/*` convention, which is this workflow's own scheme.

**Plan corrected during this phase.** Phase 0's Changes item 4 and its Acceptance line named `CalendarPoolFactory` / `CalendarPoolMembershipFactory`. That file has no factory_boy classes — it uses `create_*` functions throughout. The implementer followed the file and flagged the divergence; the plan now names `create_calendar_pool` / `create_calendar_pool_membership` so later phases do not inherit a symbol that does not exist.

## Current phase

**Phase 1 — Make roster removal non-destructive.** Base: `plan/calendar-pools/phase-0`. The one phase in this plan that changes production behavior; its PR must not merge before the client handoff it generates reaches the Web SPA and partner-integration owners.

## Deferred phases

_None._ The plan has no cross-repo phases and no flag-removal phase.
