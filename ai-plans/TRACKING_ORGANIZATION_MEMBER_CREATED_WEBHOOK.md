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

### Phase 1 — Enveloped delivery for all event types (breaking) ✅
- Status: implemented, fixed (SHOULD-FIX), verified, reviewed, pushed. PR pending (gh unavailable).
- Model: Tier 3 (sonnet).
- Branch: `plan/organization-member-created-webhook/phase-1` → base `phase-0`.
- Commits: `c1658ce` (feat envelope) + `195bdf1` (refactor: type as WebhookEnvelope + e2e retry test).
- Key correctness: envelope `id = str(event.main_event_fk_id or event.id)` (composite ForeignObject → FK accessor is `main_event_fk_id`, not `main_event_id`); retry-chain-stable.
- Verify: ruff clean; mypy no new errors; webhooks suite 58 passed; check --deploy only dev warnings.
- Review: no BLOCKER; 1 SHOULD-FIX (use WebhookEnvelope TypedDict) fixed; NITs addressed (e2e retry test, `data: dict[str, Any]`).
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-1.md` (pending).

### Phase 2 — Membership side-effects service + invitation-accept emission ✅
- Status: implemented, fixed (1 BLOCKER + 2 SHOULD-FIX), verified, reviewed, pushed. PR pending (gh unavailable).
- Model: Tier 3 (sonnet).
- Branch: `plan/organization-member-created-webhook/phase-2` → base `phase-1`.
- Commits: `e8a8807` (feat) + `0b0c748` (fix: defer to transaction.on_commit).
- BLOCKER fixed: prod ATOMIC_REQUESTS=True meant the synchronous `send_event` raced the Celery worker before commit (lost delivery). Now `transaction.on_commit` inside `on_member_created` (protects Phase 3/4 callers too). Regression test added (capture execute=False).
- Verify: ruff/mypy clean (0 new); makemigrations clean; full suite 2022 passed; check --deploy only dev warnings.
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-2.md` (pending).

### Phase 3 — Org-creator (admin) emission ✅
- Status: implemented, verified, reviewed (clean), pushed. PR pending.
- Model: Tier 2 (sonnet). Branch `phase-3` → base `phase-2`. Commit `6d61052`.
- create_organization captures admin membership + calls on_member_created (reuses Phase 2 DI + on_commit deferral). No DI/migration.
- Verify: ruff/mypy clean (0 new); full suite 2025 passed; check --deploy dev warnings only.
- Review: no BLOCKER/SHOULD-FIX; 1 NIT (redundant local import) left. Confirmed no double-emit (provision org-branch delegates to create_organization).
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-3.md` (pending).

### Phase 4 — Provision-path coverage + multi-org refire ✅
- Status: implemented, verified, reviewed (clean), pushed. PR pending.
- Model: Tier 3 (sonnet). Branch `phase-4` → base `phase-3`. Commit `cd46add`.
- One line: emit in provision_tenant_for_user pending-invitation branch (leaf). Org-creation branch left delegating (no double-emit). 3 disjoint call sites total.
- Verify: ruff/mypy clean (0 new); full suite 2030 passed; check --deploy dev warnings only.
- Review: no BLOCKER/SHOULD-FIX; 2 NITs (redundant imports; organization_name already covered in Phase 2 unit tests) left. Confirmed exactly-once + genuine multi-org scoping.
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-4.md` (pending).

### Phase 5 — GraphQL foundation: resource + WebhookConfiguration type ✅
- Status: implemented, verified, reviewed (clean), pushed. PR pending.
- Model: Tier 2 (sonnet). Branch `phase-5` → base `phase-4`. Commit `60aa200`.
- Added WEBHOOK_CONFIGURATION resource (+ cosmetic public_api 0007 migration); webhooks/graphql.py with config + read-only event types. FIELD_TO_RESOURCE_MAPPING untouched (Phase 6/7). configuration_id via configuration_fk_id.
- Verify: ruff/mypy clean (0 new); makemigrations clean; full suite 2045 passed; check --deploy dev warnings only.
- Review: no BLOCKER/SHOULD-FIX; 2 cosmetic NITs left. Types confirmed not yet in public SDL.
- Cross-plan note: public_api 0007 may collide with the parallel booking-code plan's migration number at integration time.
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-5.md` (pending).

### Phase 6 — GraphQL WebhookConfiguration CRUD ✅
- Status: implemented, fixed (3 SHOULD-FIX), verified, reviewed, pushed. PR pending.
- Model: Tier 3 (sonnet). Branch `phase-6` → base `phase-5`. Commits `61a3972` (feat) + `24f7f24` (refactor: validation→service, .live() manager, return type).
- Query + create/update/delete mutations, org-scoped, [IsAuthenticated, OrganizationResourceAccess], all 4 fields in FIELD_TO_RESOURCE_MAPPING. Reuses webhook_service. New WebhookConfiguration manager/queryset (.live()). schema.yml regenerated (incl. Phase 5's resource enum drift).
- Verify: ruff/format clean; mypy 0 new in changed files; full suite 2066 passed; makemigrations clean; check --deploy dev warnings only.
- Review: no BLOCKER (tenant isolation verified for all 4 ops); 3 SHOULD-FIX fixed; NITs left.
- PR-context: `.vinta-ai-workflows/prs-context/organization-member-created-webhook/phase-6.md` (pending).

## Current phase
- Phase 7 — GraphQL WebhookEvent history read. Tier 2 (sonnet).

## Remaining phases
- Phase 3 — Org-creator (admin) emission
- Phase 4 — Provision-path coverage + multi-org refire
- Phase 5 — GraphQL foundation: resource + WebhookConfiguration type
- Phase 6 — GraphQL WebhookConfiguration CRUD
- Phase 7 — GraphQL WebhookEvent history read

## Deferred phases
- None (no cross-repo, no flag-removal phase).
