# Tracking — Per-Owner-Scoped Public API Token Writes

- **Plan**: [ai-plans/2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_IMPLEMENTATION_PLAN.md](2026-06-18-PER_OWNER_SCOPED_PUBLIC_API_TOKEN_WRITES_IMPLEMENTATION_PLAN.md)
- **Spec**: [ai-plans/2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md](2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md)
- **Started**: 2026-06-18
- **Last updated**: 2026-06-18
- **Feature flag**: none (owner-scope guard is a no-op for org-wide tokens)

## run_options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true (reusing existing worktree)
- `worktree_path`: `.claude/worktrees/plan-per-owner-scoped-public-api-tokens`
- `worktree_branch`: `main` (the original plan's phases 1–3 merged to main mid-run — PRs #105/#106/#107 — so this plan now bases on `main`, not phase-3)
- `commit_strategy_resolved`: stacked-branches

## Branch topology
- Phase 1 base: `main` (rebased after PRs #105–107 merged)
- Branch pattern: `plan/per-owner-scoped-public-api-token-writes/phase-{id}`

## Main-health fix carried in Phase 1
`main` had a broken migration graph: two `0007` leaves in `public_api`
(`0007_systemuser_scoped_to_membership` + `0007_add_webhook_configuration_resource`, from separate
merged plans). Phase 1 adds `0008_merge_20260619_0111` + `0009_alter_resourceaccess_resource_name`
(commit `099e67e`) to repair it. Cherry-pickable as a standalone hotfix.

## Completed Phases

### Phase 1 — Owner-guard blocked-time writes ✅
- **Status**: complete, reviewed (Layers 1–3 clean; 1 SHOULD-FIX applied)
- **Model**: claude-sonnet-4-6 (plan tier 3)
- **Branch**: `plan/per-owner-scoped-public-api-token-writes/phase-1`
- **Base**: `main` (rebased; PR #138)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/138
- **Commits**: `a1e3c7b` (feat: guard), `ff9c26e` (test: id-calendar mismatch regression), `099e67e` (fix: merge conflicting 0007 migrations) — SHAs after rebase onto main
- **Final gate**: full suite 2438 passed; `makemigrations --check` clean
- **Summary**: Added shared write guard `assert_calendar_in_owner_scope(system_user, org, calendar_id)` to [public_api/scoping.py](../public_api/scoping.py) — no-op for `system_user=None` and org-wide tokens (`scoped_calendar_ids` → None), raises `Calendar.DoesNotExist("Calendar matching query does not exist.")` for a scoped token targeting an out-of-scope calendar. Wired it into `create_blocked_time`/`update_blocked_time`/`delete_blocked_time` INSIDE the existing `try/except Calendar.DoesNotExist`, so a cross-owner attempt is byte-identical to a genuinely-missing calendar (`"Calendar not found."`, success=False, no row touched). Added `CREATE/UPDATE/DELETE_BLOCKED_TIME` to `PROVIDER_SCOPED_RESOURCES`. Tests: 5 guard unit tests + 13 integration tests (success / cross-owner-indistinguishable+no-row / org-wide-unaffected / missing-grant per verb, plus a pinned id↔calendar-mismatch rejection test). Full suite 2392 passed.

### Phase 2 — Owner-guard availability writes ✅
- **Status**: complete, reviewed (Layers 1–3 clean; 2 SHOULD-FIX test-hardening applied)
- **Model**: claude-sonnet-4-6 (plan tier 3)
- **Branch**: `plan/per-owner-scoped-public-api-token-writes/phase-2` (base phase-1)
- **Commits**: `a0242e1` (feat: guard 4 availability mutations), `18d2a78` (test: harden cross-owner row-unchanged assertions)
- **Summary**: Added `assert_calendar_in_owner_scope` (the Phase-1 guard) to `create_availability_window`/`update_availability_window`/`delete_availability_window`/`batch_update_availability_windows`, inside each existing `try/except Calendar.DoesNotExist` (cross-owner → byte-identical not-found, no row touched). Confirmed `batch_update_availability_windows` uses a single top-level `calendar_id` for the whole atomic batch → one guard up front rejects a cross-owner batch wholesale with no partial write. The update/delete `available_time_id` second-vector is constrained by the service (`filter(calendar_fk=calendar)`). Added the 4 availability write resources to `PROVIDER_SCOPED_RESOURCES`. 17 integration tests (success no-rrule/rrule, cross-owner-indistinguishable+no-row, org-wide-unaffected, missing-grant per verb; batch wholesale-rejection with seeded create/update/delete ops). Full suite 2455 passed.

## Current Phase
Phase 3 — scheduleEvent mutation + owner-scoped event allowance.

## Remaining Phases
- Phase 3 — scheduleEvent mutation + owner-scoped event allowance (Tier 4)
- Phase 4 — Nested-field owner-scope sweep + security review (Tier 4)

## Deferred Phases
_(none — no cross-repo, no flag-removal)_
