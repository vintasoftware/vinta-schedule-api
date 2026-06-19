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
- `worktree_branch`: `plan/per-owner-scoped-public-api-tokens/phase-3` (this plan stacks on the original plan's phase-3 / PR #107, not main)
- `commit_strategy_resolved`: stacked-branches

## Branch topology
- Phase 1 base: `plan/per-owner-scoped-public-api-tokens/phase-3`
- Branch pattern: `plan/per-owner-scoped-public-api-token-writes/phase-{id}`

## Completed Phases

### Phase 1 — Owner-guard blocked-time writes ✅
- **Status**: complete, reviewed (Layers 1–3 clean; 1 SHOULD-FIX applied)
- **Model**: claude-sonnet-4-6 (plan tier 3)
- **Branch**: `plan/per-owner-scoped-public-api-token-writes/phase-1`
- **Base**: `plan/per-owner-scoped-public-api-tokens/phase-3`
- **Commits**: `7d345b7` (feat: guard), `61d5efb` (test: id-calendar mismatch regression)
- **Summary**: Added shared write guard `assert_calendar_in_owner_scope(system_user, org, calendar_id)` to [public_api/scoping.py](../public_api/scoping.py) — no-op for `system_user=None` and org-wide tokens (`scoped_calendar_ids` → None), raises `Calendar.DoesNotExist("Calendar matching query does not exist.")` for a scoped token targeting an out-of-scope calendar. Wired it into `create_blocked_time`/`update_blocked_time`/`delete_blocked_time` INSIDE the existing `try/except Calendar.DoesNotExist`, so a cross-owner attempt is byte-identical to a genuinely-missing calendar (`"Calendar not found."`, success=False, no row touched). Added `CREATE/UPDATE/DELETE_BLOCKED_TIME` to `PROVIDER_SCOPED_RESOURCES`. Tests: 5 guard unit tests + 13 integration tests (success / cross-owner-indistinguishable+no-row / org-wide-unaffected / missing-grant per verb, plus a pinned id↔calendar-mismatch rejection test). Full suite 2392 passed.

## Current Phase
Phase 2 — Owner-guard availability writes.

## Remaining Phases
- Phase 2 — Owner-guard availability writes (Tier 3)
- Phase 3 — scheduleEvent mutation + owner-scoped event allowance (Tier 4)
- Phase 4 — Nested-field owner-scope sweep + security review (Tier 4)

## Deferred Phases
_(none — no cross-repo, no flag-removal)_
