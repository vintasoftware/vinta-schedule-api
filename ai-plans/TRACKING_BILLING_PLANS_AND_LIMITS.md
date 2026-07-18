# Tracking — Billing Plans and Limits

- **Feature**: Billing Plans and Limits
- **Plan**: @ai-plans/2026-07-18-BILLING_PLANS_AND_LIMITS_IMPLEMENTATION_PLAN.md
- **Spec**: @ai-plans/2026-07-18-BILLING_PLANS_AND_LIMITS_SPEC.md
- **Plan id**: `BILLING_PLANS_AND_LIMITS` (kebab: `billing-plans-and-limits`)
- **Started**: 2026-07-18
- **Last updated**: 2026-07-18

## Feature flag

**None.** The plan deliberately declares no feature flag — see the "No feature flag" row in the plan's **Guiding Decisions**. The rollout switch is the plan catalog itself: every organization is seeded onto an `unlimited` plan whose `PlanLimit.limit_value` is NULL, so enforcement code runs but cannot block. Rollback for any organization is a `change_plan` back to `unlimited`, no deploy.

Consequence: there is no flag-removal phase. Instead **every enforcement phase carries a test asserting an `unlimited` organization sees unchanged behavior** — that test set is the equivalent of a flag-off suite.

## Run options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config default |
| `generate_inline_comments` | `true` | **user override** (config default `false`) |
| `full_test_suite` | `true` | **user override** (config default `false`) |
| `use_worktree` | `true` | config default |
| `commit_strategy_resolved` | `stacked-branches` | user answer (`commit_strategy: ask`) |
| `worktree_path` | `.claude/worktrees/plan-billing-plans-and-limits` | prepare-worktree |
| `worktree_branch` | `plan-billing-plans-and-limits` | prepare-worktree |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-billing-plans-and-limits.yaml` | prepare-worktree |
| `sandbox_tier` | `enforced` | probed (`sandbox-exec` present) |

`WORKROOT` = `.claude/worktrees/plan-billing-plans-and-limits`
`BASE_BRANCH` = `plan-billing-plans-and-limits` (at `0361419`)

## Agent models

Implementer per-phase from the plan's `**Suggested AI model**:` line. Others from `.vinta-ai-workflows.yaml` `agent_models`, with per-phase `**Review models**:` overrides taking precedence:

- reviewer: Tier 3 default; Tier 4 on Phases 1, 2, 4, 5, 6b, 7, 9, 11, 13, 14
- fixer: Tier 2 default; Tier 3 on Phases 5 and 13
- worktree_prep: Tier 1 (done)
- integrate: Tier 1

## Environment notes

- Local `main` is **1 commit ahead of `origin/main`** (the plan commit `0361419`, unpushed). Phase PRs will show the plan file in phase-1's diff until `main` is pushed.
- The two forked docker volumes (`..._dbdata`, `..._floci_data`) need creating before compose first boots — expected on the first `docker compose run` of Phase 1.
- A concurrent commit `0a039dc "chore: Update vinta-ai-workflows"` landed on `main` mid-session, bumping the workflows package to 0.3.0 and updating the prepare-worktree skill. Not related to this plan.

## Completed phases

_None yet._

## Current phase

**Phase 1 — Move billing ownership to the organization** (implementer Tier 3, reviewer Tier 4)

Base: `plan-billing-plans-and-limits` · Branch: `plan/billing-plans-and-limits/phase-1`

## Remaining phases

| Phase | Title | Impl | Reviewer | Fixer |
|---|---|---|---|---|
| 2 | Authenticate provider webhooks and make them idempotent | 3 | 4 | — |
| 2b | Stripe adapter behind the provider abstraction *(parallel track)* | 3 | — | — |
| 3 | Plan catalog, limits, and entitlements | 3 | — | — |
| 4 | Place every organization on a plan | 3 | 4 | — |
| 5 | Effective limits and usage counting | 4 | 4 | 3 |
| 6a | Enforce pre-paid limits: seats and invitations | 3 | — | — |
| 6b | Enforce pre-paid limits: calendars, groups, bundles, availability | 3 | 4 | — |
| 6c | Enforce pre-paid limits: webhook subscriptions and API system users | 2 | — | — |
| 7 | Meter event occurrences | 4 | 4 | — |
| 8 | Enforce the post-paid allowance | 3 | — | — |
| 9 | Upgrade, add-on purchase, and proration | 3 | 4 | — |
| 10 | Grace, dunning, and the restricted transition | 3 | — | — |
| 11 | Restricted enforcement and sync pause | 3 | 4 | — |
| 12 | Usage API and approaching-limit warnings | 2 | — | — |
| 13 | Cycle close, overage charge, and reconciliation | 4 | 4 | 3 |

## Deferred phases

**Phase 14 — Roll organizations onto real plans.** Deferred by user decision at run start.

Gated on signal this run cannot produce:
1. Phases 1–13 merged.
2. Phase 13 reconciliation running clean against the Stripe sandbox for at least one simulated cycle.
3. **The real plan limit values and paid tier numbers from product** — the plan's **Open Questions** item 4. Phase 3 seeds only `unlimited` and a placeholder `free`.

Executing it without (3) would seed invented tiers and migrate organizations onto limits nobody agreed to — the exact outcome spec objective 4 sets at zero.

No cross-repo phases in this plan. No flag-removal phase (no flag declared).
