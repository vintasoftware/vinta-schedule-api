# Tracking — Multi-Organization Membership

- **Plan:** ai-plans/2026-06-13-MULTI_ORG_MEMBERSHIP_IMPLEMENTATION_PLAN.md
- **Started:** 2026-06-13
- **Last updated:** 2026-06-13
- **Feature flag:** none (pre-production; justified in plan Guiding Decisions)

## Run options
- `commit_strategy_resolved`: stacked-branches
- `pause_between_phases`: false
- `generate_inline_comments`: true
- `use_worktree`: true
- `worktree_path`: .claude/worktrees/plan-multi-org-membership
- `worktree_branch`: plan/multi-org-membership
- `worktree_summary`: .vinta-ai-workflows/worktrees/plan-multi-org-membership.yaml
- `pr_template_used`: none (pr_template_paths empty → free-form)

## Branch naming
`plan/multi-org-membership/phase-{id}` (stacked).

## Completed phases

### Phase 1 — Membership FK migration ✅
- **Model used:** claude-sonnet-4-6 (plan tier 3) · agent: migration-author
- **Branch:** plan/multi-org-membership/phase-1 · **base:** main
- **PR:** https://github.com/vintasoftware/vinta-schedule-api/pull/66 (published, 4 inline comments)
- **Summary:** `OrganizationMembership.user` OneToOne→FK, `related_name` → `organization_memberships`, composite `unique(user, organization)` (`uniq_membership_user_organization`). Migration 0006 reversible. `get_active_organization_membership` rewired to a manager query (single active membership, `order_by("created")` fallback) — same signature, so ~60 call sites untouched. `is_organization_admin` now per-org. Provisioning guards preserved at "any membership row" (active or inactive) — Phase 4 relaxes to per-org. Swept singular reverse reads in calendar_integration + public_api + tests.
- **Review:** Layer-3 clean (no blockers); 1 SHOULD-FIX fixed (guard narrowed to active-only → restored to any-row), stale docstring + nondeterministic ordering fixed, added active-vs-inactive resolution test.
- **Gate:** ruff + format + makemigrations --check + check --deploy green; `pytest -n auto` 1555 passed, 1 sanctioned pre-existing failure (`test_send_unknown_account_sms_success`, red on base too).

## Current phase
Phase 2a — Resolver + header happy path (Tier 3 · sonnet · implementer).

## Remaining phases
- Phase 2a — Resolver + header happy path
- Phase 2b — No-header multi-org → 400
- Phase 2c — Non-member org → 403
- Phase 3 — List my orgs endpoint
- Phase 4 — Multi-org invitation accept
- Phase 5 — Create additional org

## Deferred phases
_None (no cross-repo, no flag-removal phases)._
