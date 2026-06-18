# Tracking — Single-Use Scheduling Codes

- **Plan**: ai-plans/2026-06-17-SINGLE_USE_SCHEDULING_CODES_IMPLEMENTATION_PLAN.md
- **Started**: 2026-06-17
- **Last updated**: 2026-06-18
- **Feature flag**: none (purely additive surface)

## Run options
- pause_between_phases: false
- generate_inline_comments: true
- use_worktree: true
- worktree_path: .claude/worktrees/plan-single-use-scheduling-codes
- worktree_branch: plan/single-use-scheduling-codes/wt-base
- worktree_summary: .vinta-ai-workflows/worktrees/plan-single-use-scheduling-codes.yaml
- commit_strategy_resolved: stacked-branches
- pr_creation: agents-create
- execution_model: docker-compose-in-container (`docker compose run --rm api uv run …`, COMPOSE_PROJECT_NAME=vinta-schedule)

## Notes
- Worktree rebased onto local `main` (docker-compose skill updates 868627d / af97bfa / 87fe363).
- All gates run in-container against docker postgres. Host has no `uv`; subagents commit `--no-verify`, orchestrator verifies equivalents in-container (ruff, makemigrations --check, check --deploy, full pytest, schema drift).

## Completed phases

### Phase 0 — Token lifecycle foundation ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop), outer gate green.
- **Model/tier**: migration-author / Sonnet (Tier 3).
- **Branch**: plan/single-use-scheduling-codes/phase-0 (base: wt-base).
- **Commits**: 5b71502 (foundation) + 96c52bf (review fixes).
- **Outer gate**: `check --deploy` 5 pre-existing warnings/0 errors; `pytest -n auto` 2032 passed; ruff clean; makemigrations clean; schema no drift.
- **Summary**: Added `expires_at` / `minted_by_system_user` (FK→SystemUser, SET_NULL) / `consumed_source_ip` / `calendar_group` (OrganizationForeignKey) to `CalendarManagementToken`; `CalendarManagementTokenManager` with `active()`, atomic `consume()` (transaction.atomic + select_for_update + re-check, raises domain errors), `get_token_error_code()`. `CalendarPermissionService.create_booking_token` / `validate_code` / `consume_code`. New exceptions TokenExpired/AlreadyUsed/Revoked. `PublicAPIResources.CALENDAR_BOOKING_CODE` + 7 mint/revoke field mappings. Strawberry `BookingCodeErrorCode` + `BookingCodeResult` + `CodeEventResult` (defined, not yet wired). Migrations calendar_integration/0020 + public_api/0007. Real two-thread concurrency test proves first-write-wins.
- **Key decisions**: `calendar_group` FK added (group booking codes need group binding; mirrors existing calendar/event composite-FK pattern). consume() wraps in reentrant `transaction.atomic()` so non-request callers (Celery/commands) are safe regardless of ATOMIC_REQUESTS.

### Phase 1 — Mint booking codes ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop), outer gate green.
- **Model/tier**: implementer / Sonnet (Tier 3 — Haiku first attempt failed: misread base, duplicated Phase 0 defs, wrote stray edits into the MAIN checkout which were recovered + restored; reset clean and re-ran on Sonnet).
- **Branch**: plan/single-use-scheduling-codes/phase-1 (base: phase-0).
- **Commits**: b8c6434 (mint mutations) + 2af8a3d (org-scope validation fix).
- **Outer gate**: `pytest -n auto` 2043 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: `createCalendarBookingCode` + `createCalendarGroupBookingCode` on `CalendarGroupMutations` (org-token-gated via CALENDAR_BOOKING_CODE), delegate to `create_booking_token(permissions=[CREATE])`, org from authenticated request, `minted_by` = request system user, bundle calendars transparent. Returns `BookingCodeResult{code, id}`. Not-found + organizationId-mismatch → INVALID_CODE (no cross-org leak). 11 tests.
- **PR**: pending (filled below after push).

### Phase 2 — Mint reschedule & cancel codes ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop), outer gate green.
- **Model/tier**: implementer / Sonnet (Tier 3, used over Haiku for reliability after Phase 1).
- **Branch**: plan/single-use-scheduling-codes/phase-2 (base: phase-1).
- **Commits**: 57b6eb2 (4 mint mutations) + d5e8929 (restrict calendar codes to non-grouped events).
- **Outer gate**: `pytest -n auto` 2066 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: `createCalendarRescheduleBookingCode` / `createCalendarGroupRescheduleBookingCode` (RESCHEDULE) + `createCalendarCancellationBookingCode` / `createCalendarGroupCancellationBookingCode` (CANCEL), event-bound. Validates event∈org AND event.calendar_fk_id==calendarId (calendar variants additionally require calendar_group_fk_id IS NULL → no grouped events) / event.calendar_group_fk_id==groupId (group variants). Uniform "Not found." INVALID_CODE on all failures. Shared inputs CreateEventCodeInput / CreateGroupEventCodeInput. Permission-swap guard + grouped-event-rejection tests.
- **PR**: pending (filled after push).

### Phase 3 — Revoke codes ✅
- **Status**: complete, reviewed (Layers 1–3; no BLOCKERs/SHOULD-FIX, only test-cosmetic NITs noted).
- **Model/tier**: implementer / Haiku (Tier 1 — succeeded on this small phase).
- **Branch**: plan/single-use-scheduling-codes/phase-3 (base: phase-2).
- **Commits**: dbc0f12 (revoke mutation + service method).
- **Outer gate**: `pytest -n auto` 2072 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: `CalendarPermissionService.revoke_token(org_id, token_id)` — org-scoped fetch, InvalidTokenError if absent, idempotent `revoked_at` set. `revokeBookingCode(input{organizationId,id})` mutation org-gated; unknown/cross-org/mismatch → uniform "Not found." INVALID_CODE; success returns BookingCodeResult(success=True) with NO code. Reviewer confirmed revoke sets the same `revoked_at` that consume/active/get_token_error_code check. 6 tests, DB-verified.
- **Open NITs (deferred, cosmetic)**: idempotency test docstring wording; tighten timestamp assertion to equality; late imports in test.
- **PR**: pending (filled after push).

## Current phase
Phase 4 — Code-gated availability reads (next).

## Remaining phases
- Phase 4 — Code-gated availability reads
- Phase 5a — Book single-calendar event with code
- Phase 5b — Book calendar-group event with code
- Phase 6a — Reschedule single-calendar event with code
- Phase 6b — Reschedule calendar-group event with code
- Phase 6c — Cancel event with code

## Deferred phases
_None (no cross-repo, no flag-removal)._
