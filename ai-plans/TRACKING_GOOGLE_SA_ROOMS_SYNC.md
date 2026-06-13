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

### Phase 2 — Replace broken JWT auth with DWD `service_account.Credentials` ✅
- Status: DONE. Agent: implementer. Model: sonnet (Tier 3). Reviewer: sonnet. Fixers: haiku (silent no-op — discarded), sonnet (landed).
- Commit: `63d708d feat(calendar): authenticate Google service accounts via domain-wide delegation`
- What: removed `_generate_jwt` + `from_service_account_credentials`; added `_SA_SCOPES` (both `.readonly`) + `from_service_account` classmethod (allocates via `cls.__new__(cls)`, sets `account_id`/`client` Calendar API/`admin_client` Admin SDK from one DWD cred via `.with_subject(admin_email)`); class-level `admin_client: Any` annotation for mypy; rewired `calendar_service.get_calendar_adapter_for_account` SA branch. Tests: dropped 4 obsolete JWT/old-error tests, added `test_from_service_account_builds_both_clients` (asserts both clients + payload dict + `scopes=_SA_SCOPES`), `test_from_service_account_calls_with_subject` (admin_email), error-propagation test; integration test now asserts `from_service_account` called once with `admin_email`/`email`.
- Outer gate: ruff clean, mypy −1 error vs base, makemigrations clean, check --deploy clean, `pytest -n auto` 1549 passed (lone pre-existing accounts SMS failure).
- Note: a haiku fixer reported SUCCESS but its edits never committed (silent no-op) — caught in Layer-1 re-verify; re-dispatched a sonnet fixer that landed + was verified against committed HEAD.

## Current phase
Phase 3 — Use Admin SDK to list Workspace resource calendars. Tier 3 (sonnet). Agent: implementer.

## Remaining phases
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
