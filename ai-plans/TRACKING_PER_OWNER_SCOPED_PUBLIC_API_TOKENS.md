# Tracking — Per-Owner-Scoped Public API Tokens

- **Plan**: `ai-plans/2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_IMPLEMENTATION_PLAN.md`
- **Spec**: `ai-plans/2026-06-17-PER_OWNER_SCOPED_PUBLIC_API_TOKENS_SPEC.md`
- **Started**: 2026-06-18
- **Last updated**: 2026-06-18
- **Feature flag**: none (data-gated by `scoped_to_user IS NULL`; no flag-removal phase)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
  - `worktree_path`: `.claude/worktrees/plan-per-owner-scoped-public-api-tokens`
  - `worktree_branch`: `plan/per-owner-scoped-public-api-tokens/wt`
  - `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-per-owner-scoped-public-api-tokens.yaml`
- `commit_strategy_resolved`: stacked-branches
- `pr_template_used`: none (free-form)

## Environment notes
- **Gates run INSIDE docker**, not on host. Host→localhost:5432 forwarding exhausts host ephemeral
  ports under concurrent worktrees (`Can't assign requested address`). Recipe in `WORKTREE.md` /
  worktree summary `state.test_runner.recipe`. Build gate + full suite confirmed green this way.
- Branch base rebased onto latest `main` (`87fe363`) after the docker-compose skill updates landed.

## Completed Phases

### Phase 0 — Add scoped_to_user + owner-derivation helper ✅
- **Status**: implemented, reviewed (3 layers), pushed. PR pending (no `gh`/`yq` on host — publish later).
- **Model used**: claude-haiku-4-5 (plan Tier 2).
- **Branch**: `plan/per-owner-scoped-public-api-tokens/phase-0`
- **Base**: `main`
- **PR-context**: `.vinta-ai-workflows/prs-context/per-owner-scoped-public-api-tokens/phase-0.md` (status: pending)
- **Outer gate**: `check --deploy` green (5 expected local W-warnings) + `pytest -n auto` → 2005 passed.
- **Summary**: Added `SystemUser.scoped_to_user` nullable FK to `users.User` (CASCADE, indexed;
  `NULL` = org-wide legacy default, no backfill). Migration `0007_systemuser_scoped_to_user`
  (additive AddField). New `public_api/scoping.py` `scoped_calendar_ids(system_user, organization)`
  returning `None` for org-wide (unrestricted sentinel) vs a set (possibly empty) for scoped,
  org-filtered + `.distinct()` over the `CalendarOwnership.user` edge. `PROVIDER_SCOPED_RESOURCES`
  frozenset (six provider resources, enum-member references, below the enum) for Phase 2/3 mint
  validation. `scoped_to_user` surfaced read-only in admin (owner immutable). Tests cover
  None/owned-only/empty/cross-org-isolation. Review fixes: tautological exclusion test made
  behavioral, constant drift-proofed, `.distinct()` added.
- **Deviations**: none.

## Current Phase
Phase 1 — Enforce owner scope on read queries (next).

## Remaining Phases
- Phase 1 — Enforce owner scope on read queries (Tier 3)
- Phase 2 — `createScopedSystemUser` mutation (Tier 3)
- Phase 3 — REST create accepts optional owner (Tier 2)
- Phase 4a — `createAvailableTime` mutation, owner-guarded (Tier 3)
- Phase 4b — `createBlockedTime` mutation, owner-guarded (Tier 2)
- Phase 4c — `scheduleEvent` mutation, owner-guarded (Tier 3)
- Phase 5 — Cross-owner adversarial sweep + security review (Tier 4)

## Deferred Phases
None (no cross-repo, no flag-removal phase).
