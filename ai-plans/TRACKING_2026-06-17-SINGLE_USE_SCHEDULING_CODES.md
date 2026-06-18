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

### Phase 4 — Code-gated availability reads ✅
- **Status**: complete, reviewed (Layers 1–3 security review + fix loop; no BLOCKERs).
- **Model/tier**: implementer / Sonnet (Tier 3).
- **Branch**: plan/single-use-scheduling-codes/phase-4 (base: phase-3).
- **Commits**: 21b8c09 (5 code-gated read fields + resolve_code) + abab4a5 (harden: hash-verify test, range clamp, org guard).
- **Outer gate**: `pytest -n auto` 2102 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: FIRST unauthenticated surface. `CalendarPermissionService.resolve_code(code)` derives org from the code (unscoped `original_manager` lookup gated by constant-time hash verify); `validate_code` delegates + org-asserts. Five no-permission_classes query fields (availableTimesWithCode, availabilityWindowsWithCode, unavailableWindowsWithCode, calendarGroupBookableSlotsWithCode, calendarGroupAvailabilityWithCode) — scope strictly from token (calendar/calendar_group or event.calendar/event.calendar_group), never consume, uniform "Invalid or expired code." on all failures (no oracle), datetime range clamped to 366 days (DoS guard). Reviewer confirmed: hash-verify-before-use, scope confinement, cross-org isolation, non-consumption. Rate limiter already keys unauth as anon:<client_ip>. 30 tests.
- **NOTE**: main checkout has UNRELATED in-progress work (validateReturnUrl OAuth feature) on public_api/{queries,types,tests}; left untouched — worktree isolation kept my work separate.
- **PR**: pending (filled after push).

### Phase 5a — Book single-calendar event with code ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop). Layer 3 caught a BLOCKER — fixed.
- **Model/tier**: implementer / Sonnet (Tier 3).
- **Branch**: plan/single-use-scheduling-codes/phase-5a (base: phase-4).
- **Commits**: e0288b7 (book-with-code mutation) + c9d6d72 (BLOCKER fix: authorize on restricted calendars).
- **Outer gate**: `pytest -n auto` 2114 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: unauthenticated `createCalendarEventWithCode` (no permission_classes). resolve_code → require CREATE perm + calendar scope → atomic { initialize_without_provider(user_or_token=code) → create_event → consume_code }. First-write-wins (consume after create, under lock, rolls back on race). SLOT_UNAVAILABLE ← NoAvailableTimeWindowsError/EventManagementError (code NOT consumed, retryable); PermissionDenied → NOT_PERMITTED; resolve_code errors → INVALID_CODE/EXPIRED/ALREADY_USED/REVOKED. IP audit from X-Forwarded-For/REMOTE_ADDR.
- **BLOCKER caught + fixed**: original impl passed `user_or_token=None`, so `can_perform_scheduling` rejected bookings on RESTRICTED calendars (the core use case) — masked by tests using accepts_public_scheduling=True. Fix: thread the code as `user_or_token` so the event service's permission instance gets the token → CREATE-permission branch authorizes on restricted calendars. Tests rewritten to use a restricted calendar with seeded availability + a real (non-mocked) slot-unavailable test.
- **PR**: pending (filled after push).

### Phase 5b — Book calendar-group event with code ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop; no BLOCKERs).
- **Model/tier**: implementer / Sonnet (Tier 3).
- **Branch**: plan/single-use-scheduling-codes/phase-5b (base: phase-5a).
- **Commits**: 5a910c4 (book-group-with-code + can_perform_scheduling group branch) + 3224907 (cross-org coverage + IP helper).
- **Outer gate**: `pytest -n auto` 2133 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: unauthenticated `createCalendarGroupEventWithCode`. Required extending `can_perform_scheduling` with a GROUP-scoped branch (token group-scoped + CREATE + calendar is a member of the group's slots, org-scoped via CalendarGroupSlotMembership) — because `create_grouped_event` books on the primary calendar and a group code has calendar_fk_id=None. Mutation: resolve_code → require CREATE + group scope → atomic { calendar_service.initialize_without_provider(user_or_token=code) → wire group_service.calendar_service=calendar_service → group_service.initialize → create_grouped_event → consume_code }. Real restricted-primary-calendar tests + real cross-org isolation (org-A code can't book org-B calendar → SLOT_UNAVAILABLE, not consumed). Shared `_client_ip_from_request` helper. 17+ tests.
- **PR**: pending (filled after push).

### Phase 6a — Reschedule single-calendar event with code ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop; no BLOCKERs).
- **Model/tier**: implementer / Sonnet (Tier 3).
- **Branch**: plan/single-use-scheduling-codes/phase-6a (base: phase-5b).
- **Commits**: a787454 (reschedule-with-code mutation + GeneratedField fix) + ef29b50 (scope availability check to code path).
- **Outer gate**: `pytest -n auto` 2146 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: unauthenticated `rescheduleCalendarEventWithCode`. resolve_code → require RESCHEDULE + event scope + single-calendar (token.calendar_group is None → group codes route to 6b). calendar_id/event_id strictly from token. Rebuilds CalendarEventInputData PRESERVING title/description/attendances/external_attendances (so can_perform_update requires only {RESCHEDULE}, not UPDATE_DETAILS/ATTENDEES), overriding only times. Atomic update→consume. Availability pre-check in the mutation (scoped to code path).
- **Notable**: found + fixed a genuine pre-existing bug — `update_event` assigned to `start_time`/`end_time` which are read-only GeneratedFields (db-derived), so reschedules never persisted; fix writes `*_tz_unaware`+`timezone` (kept in shared update_event, matches create_event). A reviewer-flagged availability check that was added to shared update_event (would regress REST/bundle updates) was MOVED into the mutation.
- **PR**: pending (filled after push).

### Phase 6b — Reschedule calendar-group event with code ✅
- **Status**: complete, reviewed (Layers 1–3 + fix loop). Layer 3 caught a tz BLOCKER — fixed.
- **Model/tier**: implementer / Sonnet (Tier 3).
- **Branch**: plan/single-use-scheduling-codes/phase-6b (base: phase-6a).
- **Commits**: 0434b01 (reschedule-group-with-code + reschedule_grouped_event) + 52c7d06 (align blocked-time tz storage on create+reschedule).
- **Outer gate**: `pytest -n auto` 2161 passed; check --deploy clean; makemigrations clean; ruff clean.
- **Summary**: unauthenticated `rescheduleCalendarGroupEventWithCode`. New `CalendarGroupService.reschedule_grouped_event(event_id, times)` updates the primary event (details-preserved → only RESCHEDULE required) + the linked non-primary BlockedTimes (`external_id` LIKE `group-event-{id}-cal-`), preserving the event id (Building Blocks linkage). Mutation: resolve_code → require RESCHEDULE + event + GROUP scope (calendar-only codes → 6a) → availability pre-check (code path) → atomic update→consume (wires group_service.calendar_service to the code-bearing instance). Time-only v1; slot re-selection deferred (Open Question 3).
- **BLOCKER caught + fixed**: tz storage divergence — `_create_non_primary_blocked_times` wrote blocked-time `*_tz_unaware` RAW (UTC wall-clock) while the primary event + reschedule write CONVERTED (local wall-clock), so non-primary blocked times drifted off the primary event for non-UTC zones (pre-existing create bug). Fixed create to convert; added a non-UTC (America/Recife) regression test asserting primary event + blocked times stay aligned.
- **PR**: pending (filled after push).

## Current phase
Phase 6c — Cancel event with code (next, final implementable phase).

## Remaining phases
- Phase 6c — Cancel event with code

## Deferred phases
_None (no cross-repo, no flag-removal)._
