# Tracking — GOOGLE_SA_ROOMS_SYNC

- **Feature**: Google Service Account Rooms Sync Fix
- **Plan**: `ai-plans/2026-06-09-GOOGLE_SA_ROOMS_SYNC_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-13
- **Last updated**: 2026-06-13
- **Feature flag**: none (plan explicitly forbids one — rooms sync has never worked)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `commit_strategy_resolved`: modular-commits
- `plan_branch`: plan/google-sa-rooms-sync
- `worktree_path`: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-google-sa-rooms-sync
- `worktree_branch`: plan/google-sa-rooms-sync
- `worktree_summary`: .vinta-ai-workflows/worktrees/plan-google-sa-rooms-sync.yaml

## PR
- #64 — https://github.com/vintasoftware/vinta-schedule-api/pull/64 (one PR for whole plan; base `main`)

## Completed phases
### Phase 1 — Rename `audience` → `admin_email` (model/API/service) ✅
- Status: DONE. Agent: migration-author. Model used: haiku (plan tier: Tier 1). Fixer: haiku. Reviewer: sonnet.
- Commit: `032e1af feat(calendar,organizations): rename service account audience field to admin_email`
- What: renamed the field across model + migration 0018 (RenameField + AlterField, `max_length=255` kept → catalog-only, no narrowing) + both Google SA serializers + the two `ServiceAccount` ModelSerializers + org PATCH view + adapter `GoogleServiceAccountCredentialsTypedDict` + `calendar_service` credentials dict + all fixtures/assertions. Read serializer field → `EmailField` (correct `format: email`). No auth behaviour change. `public_key` retained.
- Outer gate: ruff clean, makemigrations --check clean, check --deploy (5 pre-existing warnings), `pytest -n auto` 1550 passed. Pre-existing unrelated failure: `accounts/...test_send_unknown_account_sms_success` (broken on `main`, mocks removed `get_twilio_client`).
- Deferred should-fixes (→ Phase 2, where the code is deleted): (a) `from_service_account_credentials` feeds `admin_email` into the legacy JWT `aud`; (b) `test_generate_jwt` assertion is structural not behavioural. Both methods/tests are removed in Phase 2 per the plan — fixing in P1 would be churn.

## Current phase
Phase 2 — Replace broken JWT auth with `google.oauth2.service_account.Credentials` + DWD. Tier 3 (sonnet). Agent: implementer.

## Remaining phases
- Phase 2 — Replace broken JWT auth with `google.oauth2.service_account.Credentials` + DWD. Tier 3.
- Phase 3 — Use Admin SDK to list Workspace resource calendars. Tier 3.

## Deferred phases
_None — no cross-repo or flag-removal phases in this plan._

## Notes
- Worktree provisioned, smoke-tested clean (lint + Django check + 18 tests pass on isolated test DB).
- Plan doc committed onto the plan branch (was untracked in main).
- Side fix (unrelated to this plan): project sub-agent frontmatter was invalid YAML → PR #63
  `fix(ai-tools): emit YAML-safe agent description frontmatter` on branch
  `chore/fix-agent-frontmatter-yaml`. Must be merged to `main` before the real
  `migration-author`/`implementer`/`reviewer`/`fixer` agents register on restart.
