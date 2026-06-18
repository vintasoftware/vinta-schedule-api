# Tracking — organization_member_created webhook

- **Plan**: `ai-plans/2026-06-17-ORGANIZATION_MEMBER_CREATED_WEBHOOK_IMPLEMENTATION_PLAN.md`
- **Spec**: `ai-plans/2026-06-17-ORGANIZATION_MEMBER_CREATED_WEBHOOK_SPEC.md`
- **Started**: 2026-06-17
- **Last updated**: 2026-06-18
- **Feature flag**: none (hard envelope cutover per Guiding Decisions)

## Run options
- pause_between_phases: false (auto-flow)
- generate_inline_comments: true
- use_worktree: true
- commit_strategy_resolved: stacked-branches
- worktree_path: `.claude/worktrees/plan-organization-member-created-webhook`
- worktree_branch: `plan/organization-member-created-webhook/wt-base` (redundant; phase branches stack off `main`)
- worktree_summary: `.vinta-ai-workflows/worktrees/plan-organization-member-created-webhook.yaml`
- **execution: DOCKER** — all lint/test/build/migrate run via `./dr` wrapper inside the `api` image on the `vinta-schedule_default` compose network against the forked Docker DB. Host loopback ports are unreliable; do NOT use host `uv run`. Commits use `--no-verify` (host pre-commit hooks need `uv` which isn't on their PATH; gates run explicitly via `./dr`).

## Completed phases

### Phase 0 — Add event type + payload/envelope types ✅
- Status: implemented, verified, reviewed (clean), pushed. PR pending (gh unavailable).
- Model: Tier 1 (haiku) — implemented in a prior turn.
- Branch: `plan/organization-member-created-webhook/phase-0` → base `main`.
- Commits: `36e1a9d` (feat) + docs(spec+plan) + tracking.
- Verify: makemigrations --check clean; ruff clean; webhooks suite 51 passed; check --deploy only dev warnings.
- Review: no BLOCKER/SHOULD-FIX; 3 NITs (sentence-case label is spec-verbatim; `data: dict` matches plan; unneeded django_db marker) — left as-is.
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-0.md` (status: pending).

## Current phase
- Phase 1 — Enveloped delivery for all event types (breaking). Tier 3 (sonnet).

## Remaining phases
- Phase 1 — Enveloped delivery for all event types (breaking)
- Phase 2 — Membership side-effects service + invitation-accept emission
- Phase 3 — Org-creator (admin) emission
- Phase 4 — Provision-path coverage + multi-org refire
- Phase 5 — GraphQL foundation: resource + WebhookConfiguration type
- Phase 6 — GraphQL WebhookConfiguration CRUD
- Phase 7 — GraphQL WebhookEvent history read

## Deferred phases
- None (no cross-repo, no flag-removal phase).
