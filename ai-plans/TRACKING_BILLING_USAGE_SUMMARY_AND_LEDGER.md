# Tracking — Billing Usage Summary & Occurrence Ledger

**Plan**: `ai-plans/2026-08-08-BILLING_USAGE_SUMMARY_AND_LEDGER_IMPLEMENTATION_PLAN.md`
**Plan id**: `billing-usage-summary-and-ledger`
**Started**: 2026-08-08
**Last updated**: 2026-08-08

**Feature flag**: none. The plan's **Guiding Decisions** record the reason — this repo has no flag framework and is pre-production. No flag-removal phase exists.

## Run options

```yaml
run_options:
  pause_between_phases: false
  generate_inline_comments: false
  full_test_suite: true          # overrides config default; Phase 1 touches enforcement counters app-wide
  use_worktree: true
  worktree_path: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-billing-usage-summary-and-ledger
  worktree_branch: plan-billing-usage-summary-and-ledger
  worktree_summary: .vinta-ai-workflows/worktrees/plan-billing-usage-summary-and-ledger.yaml
  sandbox_tier: enforced
  commit_strategy_resolved: stacked-branches

agent_models:                    # from .vinta-ai-workflows.yaml
  reviewer: 3                    # phases 1 and 2 override to 4; phase 5 to 3
  fixer: 2
  worktree_prep: 1
  integrate: 1
```

`BASE_BRANCH` = `plan-billing-usage-summary-and-ledger`. Phase 0 branches off it; every later phase branches off the previous phase's branch.

## Completed phases

_None yet._

## Current phase

**Phase 0 — Add the billing period statement models** (implementer Tier 2, no review override).

## Remaining phases

| Phase | Title | Implementer | Review override |
|---|---|---|---|
| 1 | Per-organization breakdown in the usage counters | Tier 3 | reviewer Tier 4 |
| 2 | Persist the statement at cycle close | Tier 3 | reviewer Tier 4 |
| 3 | Enrich the current-usage summary | Tier 3 | — |
| 4 | Closed-period statement endpoints | Tier 2 | — |
| 5 | The occurrence ledger endpoint | Tier 3 | reviewer Tier 3 |

## Deferred phases

_None._ The plan has no cross-repo phases and no flag-removal phase.

## Notes

- The plan `.md` is untracked in the main checkout and was copied into the worktree. **Phase 0 commits it** alongside its own changes, so it lands with the first PR.
- Phase 4 is bundled (list + detail endpoints) per the plan's chosen granularity; that is intentional, not a phase that should be split.
- Phase 4's endpoints return empty lists until Phase 2 has run at least one cycle close in a real environment. That is expected and is documented in the plan's **Risk & Rollout Notes** as forward-only behavior, not a defect.
