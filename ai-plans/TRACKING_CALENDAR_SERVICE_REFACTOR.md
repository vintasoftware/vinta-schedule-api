# Tracking — Calendar Service Refactor

- **Plan**: `ai-plans/2026-06-13-CALENDAR_SERVICE_REFACTOR_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-13
- **Last updated**: 2026-06-13
- **Feature flag**: none (pure refactor)

## Run options
- `commit_strategy_resolved`: stacked-branches
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
  - `worktree_path`: `.claude/worktrees/plan-calendar-service-refactor`
  - `worktree_branch`: `plan/calendar-service-refactor/wt`
  - `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-calendar-service-refactor.yaml`
- Branch pattern: `plan/calendar-service-refactor/phase-{id}` (stacked, base `…/wt`)

## Completed Phases

### Phase 0 — Shared context, utils module, and lru_cache fix ✅
- **Status**: complete, PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 3) + fixer (claude-sonnet-4-6)
- **Branch**: `plan/calendar-service-refactor/phase-0` (base `plan/calendar-service-refactor/wt`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/65 (status=published, 5 inline comments)
- **Files**: `calendar_integration/services/calendar_service_context.py` (new), `calendar_integration/services/calendar_service_utils.py` (new), `calendar_integration/services/calendar_service.py` (edited), `calendar_integration/tests/services/test_calendar_service_utils.py` (new)
- **Summary**: Added `CalendarServiceContext` frozen dataclass (built once in `authenticate()`/`initialize_without_provider()`, consumed by sub-services in later phases). Moved cross-cutting helpers (timezone conversion, event serialization family, permission-granting, calendar lookups) into `calendar_service_utils.py` as module-level functions; facade methods delegate to them as thin wrappers. Fixed the multi-tenant `lru_cache` bug: calendar lookups now use a per-instance dict keyed on `(organization_id, id_or_external_id)`, reset on every (re)auth — replacing `@lru_cache` which could leak another org's Calendar. Regression test added.
- **Review**: Layer-3 found 0 blockers; fixer reverted an unsanctioned behavior change in `serialize_event_data_input` (restored byte-for-byte, pinned the preserved latent bug with a test), hoisted late imports, added the mandated delegation test + resource-path test.
- **Gate**: calendar_integration suite 1014 passed; full suite 1563 passed + 1 pre-existing unrelated failure (`accounts/.../test_send_unknown_account_sms_success`).
- **Carry-forward notes**:
  - `CalendarServiceContext` is built but NOT yet consumed — Phase 2+ sub-services receive it (perf guardrail: authenticate once).
  - Per-instance calendar cache is unbounded (vs old `maxsize=128`) — acceptable now; revisit if a long-lived sync iterates thousands of calendars.
  - Latent bug in `serialize_event_data_input` resources branch deliberately preserved + pinned by a test; a real fix is out of scope (candidate Open-Questions follow-up).

## Current Phase
- Phase 1 — Extract `RecurrenceManager` helper (next).

## Remaining Phases
- Phase 1 — Extract `RecurrenceManager` helper (Tier 4)
- Phase 2 — Extract `CalendarEventService` (Tier 4)
- Phase 3 — Extract `CalendarBundleService` (Tier 3)
- Phase 4 — Extract `AvailabilityService` (Tier 4)
- Phase 5 — Extract `CalendarSyncService` (Tier 4)
- Phase 6 — Extract `CalendarWebhookService` (Tier 3)
- Phase 7 — Shrink the facade and finalize wiring (Tier 2)

## Deferred Phases
- None (no cross-repo phases; no feature-flag-removal phase — pure refactor).
