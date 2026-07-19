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

### Phase 1 — Move billing ownership to the organization ✅

- **Status**: merged-ready, PR open
- **Models**: implementer Tier 3 (Sonnet), reviewer Tier 4 (Opus), fixer Tier 2→Sonnet (stepped up, >3 files)
- **Branch**: `plan/billing-plans-and-limits/phase-1` · **Base**: `main`
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/189
- **Commits** (after rebase onto `3755e03`): `26b6be3` tracking, `a89a9a2` implementation, `51aa7a5` review fixes

Summary:

`BillingProfile` moved from a `User` primary key to an `Organization` one — the spec's named one-way door, landed before any money can flow. `Subscription` gained real `organization`, `plan`, `billing_state`, `billing_interval`, period, grace, `plan_external_id`, and `payment_provider` fields. The dead seam was repaired: the phantom `membership` annotation and the `AttributeError`-raising `Subscription.plan` property are gone, `RefundStatusUpdate` uses `RefundStatuses` with a working `__str__`, and both `from venv import logger` imports are fixed.

Review raised **three BLOCKERs**, all fixed:
1. The payer dataclass emitted a null `payer.email`, which MercadoPago hard-400s — every payment path would have failed against the live gateway while the suite stayed green, because nothing tested the serialized payload.
2. An unguarded reverse 1:1 (`subscription.organization.billing_profile`) would 500-loop the unauthenticated provider webhook once Phase 4 gives every org a subscription while most have no billing profile. **A Phase 1 change that only detonates on Phase 4 data** — Phase 1's own tests could not have caught it.
3. `request.organization` is `None` for a user with zero memberships → `IntegrityError` → 500 on write. A genuine regression this phase introduced.

Also: the cross-organization isolation test was vacuous (passed because no profile existed; would have passed against the old user-keyed code) and was replaced with real isolation, active-org-header switching, and non-member-org cases.

**Decisions taken during the phase:**
- Added `contact_first_name`/`contact_last_name`/`contact_email`/`contact_phone` to `BillingProfile` (user decision). Provider-payload identity, deliberately distinct from `is_billing_owner`, which is about permissions. Landed here because the table was already being recreated — later would cost a second migration. Plan's **Data Model Changes** amended.
- Write actions gated on `IsOrganizationAdmin`. This phase widened the resource from "a user's own billing data" to "the organization's tax document number", so any member could read/overwrite it. `IsBillingOwnerOrAdmin` remains Phase 9's job.
- Minimal `BillingPlan` pulled forward from Phase 3 (`Subscription.plan` cannot reference a nonexistent model). Phase 3 extends additively — no destructive redo. Its DB constraint ships with a test.
- `Subscription` carries both `status` (provider-reported) and `billing_state` (internal lifecycle). Boundary documented in the model docstring; `status` dropped from the serializer.

**Verification** (orchestrator re-ran every gate independently, not relayed): full suite **3693 passed** (baseline 3679), ruff clean, mypy at the exact 322-error baseline with zero in `payments/`, `makemigrations --check` clean, migration verified reversible *after* the fixer edited `0003` in place.

**Carried forward — needs human action:**
- ⚠️ **Client-breaking**: billing-profile `id` changes source from `user_id` to `organization_id`. Same name and type, so **no schema diff** — a client that persisted the old id silently reads a different entity. Needs `handoff-to-client` before merge.
- ⚠️ **Verify in each environment** that `payments_billingprofile` and `payments_subscription` are empty before this deploys. The emptiness claim came from reading migrations and grepping for writes; nobody queried a live database. The migration fails loudly rather than losing rows, but confirm by hand.
- `MissingBillingProfileError`'s default message still says "User does not have a billing profile" — stale post-Phase-1 wording, cosmetic.

## Current phase

**Phase 2 — Authenticate provider webhooks and make them idempotent** (implementer Tier 3, reviewer Tier 4)

Base: `plan/billing-plans-and-limits/phase-1` · Branch: `plan/billing-plans-and-limits/phase-2`

## Remaining phases

| Phase | Title | Impl | Reviewer | Fixer |
|---|---|---|---|---|
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
