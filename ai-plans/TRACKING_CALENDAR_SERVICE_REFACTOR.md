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

### Phase 1 — Extract RecurrenceManager helper ✅
- **Status**: complete, PR open
- **Model**: claude-opus-4-7 (plan tier: Tier 4) + fixer (claude-sonnet-4-6)
- **Branch**: `plan/calendar-service-refactor/phase-1` (base `…/phase-0`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/68 (published, 3 inline comments)
- **Files**: `calendar_integration/services/recurrence_manager.py` (new), `calendar_integration/services/calendar_service.py` (edited, −222 lines), `calendar_integration/tests/services/test_recurrence_manager.py` (new, 9 tests)
- **Summary**: Extracted the two generic recurrence engines (`create_recurring_exception_generic`, `create_recurring_bulk_modification_generic`) into a stateless `RecurrenceManager`. Engines take `CalendarServiceContext` as first param (only `self` use was the auth guard); per-type truncate/continuation/record callbacks stay caller-supplied. Facade delegates at 6 call sites; engine methods deleted. Bodies moved byte-for-byte (reviewer diff-verified).
- **Review**: Layer-3 0 blockers (engine bodies byte-identical, guard equivalent, call sites correct). Fixer added a direct master-date exception-branch test.
- **Gate**: calendar suite 1023 passed; full suite 1572 passed + 1 pre-existing unrelated failure.
- **Carry-forward notes**:
  - `RecurrenceManager` is stateless + never imports `CalendarService` — Phase 2 (`CalendarEventService`) and Phase 4 (`AvailabilityService`) will delegate to it for their recurrence methods, passing the context.
  - Bare-`raise` defensive lines in the guard blocks are verbatim-preserved dead code (guard raises internally first) — leave until Phase 7 if ever.

### Phase 2 — Extract CalendarEventService ✅
- **Status**: complete, PR open
- **Model**: claude-opus-4-7 (plan tier: Tier 4) + fixer (claude-sonnet-4-6, mypy narrowing)
- **Branch**: `plan/calendar-service-refactor/phase-2` (base `…/phase-1`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/72 (published, 4 inline comments)
- **Files**: `calendar_integration/services/calendar_event_service.py` (new, ~1410 lines), `calendar_integration/services/calendar_service.py` (edited, −1000 lines), `calendar_integration/tests/services/test_calendar_event_service.py` (new)
- **Summary**: Moved 11 event methods (single + recurring CRUD, transfer, expansion reads, event recurrence exception/bulk-mod) into `CalendarEventService`. Facade delegates via `self._get_event_service()`. Bodies byte-for-byte; `@transaction.atomic()` moved with create/update/delete.
- **Key design — host seam**: service reaches not-yet-extracted collaborators via an `EventServiceHost` Protocol (facade passed as `host=self`): bundle fan-out (Phase 3), `get_availability_windows_in_range` (Phase 4), shared write-adapter/permission helpers. Chosen over a frozen snapshot because existing tests patch these on the facade instance + mutate account/adapter post-auth. `_get_event_service()` rebuilds a context snapshot per call (cheap dataclass, NO re-auth/adapter rebuild → perf guardrail holds); shares `_calendar_cache` + `RecurrenceManager` by reference.
- **Review**: Layer-3 0 blockers (bodies byte-identical, atomic preserved, transfer_event host-routing == original call graph, snapshot parity confirmed). Fixer fixed a ~30-error mypy `union-attr` regression by narrowing a bare `context = cast(...)` local through the TypeGuards (runtime-identical; file mypy 50→20, remainder pre-existing).
- **Gate**: full suite 1579 passed, 0 failed (after rebase onto main's SMS fix).
- **Carry-forward notes (CRITICAL for Phase 3/4)**:
  - Phase 3 (`CalendarBundleService`) must be supplied to the event service in place of the host's `_create_bundle_event`/`_update_bundle_event`/`_delete_bundle_event` methods — the seam is the `EventServiceHost` protocol; update `_get_event_service()` wiring + the host methods, do NOT change `CalendarEventService` call sites.
  - Phase 4 (`AvailabilityService`) similarly replaces the host's `get_availability_windows_in_range`.
  - Bundle helpers `_create_bundle_event`/`_update_bundle_event`/`_delete_bundle_event` still live on the facade — these are what Phase 3 extracts.
  - The mypy-narrowing pattern (`context = cast("BaseCalendarService", self._context)` then guard) is the standard for sub-service methods reading context fields — reuse it in Phases 3-6.

## Rebase log
- 2026-06-13: rebased the whole stack (`wt`→`phase-0`→`phase-1`→`phase-2`) onto `origin/main` (3286b69) after the twilio/SMS-adapter test fix landed on main (commit "fix(account-adapter): update SMS notification method to use notification service"). Conflict-free. Force-pushed `wt`/`phase-0`/`phase-1`; PRs #65/#68 auto-updated. The previously-noted "1 pre-existing failure" is now resolved on main — full suite is 0 failures from Phase 2 onward.

## Current Phase
- Phase 3 — Extract `CalendarBundleService` (next).

## Remaining Phases
- Phase 3 — Extract `CalendarBundleService` (Tier 3)
- Phase 4 — Extract `AvailabilityService` (Tier 4)
- Phase 5 — Extract `CalendarSyncService` (Tier 4)
- Phase 6 — Extract `CalendarWebhookService` (Tier 3)
- Phase 7 — Shrink the facade and finalize wiring (Tier 2)

## Deferred Phases
- None (no cross-repo phases; no feature-flag-removal phase — pure refactor).
