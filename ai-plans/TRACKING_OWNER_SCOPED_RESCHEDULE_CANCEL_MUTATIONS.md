# Tracking — Owner-Scoped Public Reschedule / Cancel Mutations

- **Plan:** `ai-plans/2026-06-22-OWNER_SCOPED_RESCHEDULE_CANCEL_MUTATIONS_IMPLEMENTATION_PLAN.md`
- **Plan id (kebab):** `owner-scoped-reschedule-cancel-mutations`
- **Started:** 2026-06-22
- **Last updated:** 2026-06-22
- **Feature flag:** none (purely additive surface)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `worktree_path`: `/Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-owner-scoped-reschedule-cancel-mutations`
- `worktree_branch`: `plan/owner-scoped-reschedule-cancel-mutations-wt`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-owner-scoped-reschedule-cancel-mutations.yaml`
- `commit_strategy_resolved`: stacked-branches
- `pr_creation`: agents-create
- `pr_template_used`: none (pr_template_paths empty → free-form description)

## Phase pipeline
| Phase | Title | Tier | Status |
|---|---|---|---|
| 0a | Service write-allowance for public tokens | 3 | ✅ done |
| 0b | Single-occurrence exception service methods | 4 | ✅ done |
| 1 | `rescheduleCalendarEvent` mutation | 3 | ✅ done |
| 2 | `rescheduleCalendarGroupEvent` mutation | 3 | ✅ done |
| 3 | `cancelEvent` mutation | 3 | ✅ done |

Deferred: none (no cross-repo phases, no flag-removal phase).

## Completed Phases

### Phase 0a — Service write-allowance for public tokens ✅
- **Model used:** sonnet (plan tier 3). **Branch:** `plan/owner-scoped-reschedule-cancel-mutations/phase-0a` (base `…-wt`).
- **Commits:** `c0deed4` (feat allowance), `bbc705f` (cross-tenant defense-in-depth + bundle doc), `91cfb80` (actor attribution).
- **What shipped:** new `_public_token_may_write(system_user, calendar)` seam on `CalendarEventService` — org-wide tokens allowed iff `system_user.organization_id == calendar.organization_id` (self-derived tenant guard); owner-scoped tokens allowed iff `_scoped_system_user_owns_calendar`. Wired into `update_event` + `delete_event`, replacing the blanket `SystemUser` rejection. `is_public_token_write` bypasses the `can_perform_update` token check for the sanctioned public path (mirrors `create_event`'s owner-scoped bypass). Cross-owner → `PermissionDenied("Calendar matching query does not exist.")` (not-found parity, no existence leak). Scoped tokens rejected on BUNDLE calendars; org-wide follow existing bundle rules. Side-effects/webhook actor on the public-token path now falls back to the `SystemUser` caller (parity with `create_event`) instead of being actor-less.
- **`create_event` stays org-wide-blocked** — pinned by regression test.
- **Tests:** 12 service-layer tests in `calendar_integration/tests/services/test_calendar_event_service.py` (owner-scoped allow, cross-owner deny w/ parity msg, org-wide allow, cross-tenant org-wide deny, create-stays-blocked regression, scoped bundle reject, Django-user path unchanged, audit-actor = SystemUser for update + delete).
- **Review:** Layer 1/2/3 clean. Reviewer found no BLOCKERs; 3 SHOULD-FIX (cross-tenant guard + test, org-wide-bundle test) + 1 NIT — guard + tests applied; org-wide-bundle test skipped (heavy bundle fan-out) with documenting comment. 3 known xdist isolation flakes verified passing individually.
- **Outer gate:** `manage.py check --deploy` clean; `pytest -n auto` → 3150 passed.
- **For later phases:** `update_event`/`delete_event` now accept owner-scoped + org-wide `SystemUser` tokens. Cross-owner/cross-tenant denial message is `"Calendar matching query does not exist."`. Phase 0b adds the single-occurrence service methods; Phases 1–3 wrap these in GraphQL.

### Phase 0b — Single-occurrence exception service methods ✅
- **Model used:** opus (plan tier 4). **Branch:** `plan/owner-scoped-reschedule-cancel-mutations/phase-0b` (stacked on phase-0a).
- **Commits:** `8d0ef9e` (feat methods), `6400174` (fix: bundle guard, external sync update-in-place, expansion tests).
- **What shipped:** `CalendarEventService.reschedule_event_occurrence(calendar_id, master_event_id, recurrence_id, start, end, tz)` and `cancel_event_occurrence(calendar_id, master_event_id, recurrence_id)`, plus shared loader `_load_recurring_master_for_occurrence` and `CalendarService` facade delegators. Single occurrence addressed by `recurrence_id` (occurrence's original start). Reschedule builds a modified-occurrence `CalendarEvent` directly (NOT via `create_event`, so org-wide isn't re-blocked) and links via `master.create_exception(exception_date=recurrence_id, is_cancelled=False, modified_object=…)`; **idempotent update-in-place** on repeat (reuses `external_id` via `write_adapter.update_event` → no external orphan). Cancel creates a cancellation exception (`is_cancelled=True`, no modified_event). Authorized via Phase 0a `_public_token_may_write` (org-wide + owner-scoped). Cross-owner/cross-tenant → not-found parity. Non-recurring master → `ValueError`. BUNDLE rejected for scoped tokens (mirrors Phase 0a). `recurrence_id` on the modified event = ORIGINAL start (matches `exception_date`; deliberate divergence from `create_event`, commented).
- **Tests:** Phase 0b tests in `calendar_integration/tests/services/test_calendar_event_service.py` — exception-row shape, master/rule untouched, idempotency (one row on double reschedule), org-wide allow, cross-owner deny, non-recurring `ValueError`, BUNDLE block, adapter-path (create then update-in-place, no orphan), and `get_occurrences_in_range` expansion (modified occurrence at new time; cancelled occurrence omitted).
- **Review:** Layer 1/2/3 clean. No BLOCKERs; SHOULD-FIX (external orphan → update-in-place; missing BUNDLE guard; adapter/expansion tests) all applied; NIT recurrence_id comment added; NIT bare-`raise` left (house idiom).
- **Outer gate:** serial scoped module 35 passed; clean full `pytest -n auto` → **3161 passed**. NOTE: shared compose `db` is contended by concurrent worktree suites — full-suite `EEEE` clusters at teardown are ENVIRONMENTAL; verify suspects via serial `-p no:randomly`.
- **For later phases:** Phase 1 wraps `reschedule_event_occurrence` (when `recurrenceId` given) + `update_event` (whole/series) for `rescheduleCalendarEvent`. Phase 3 wraps `cancel_event_occurrence` (`recurrenceId`) + `delete_event` (`deleteSeries`) for `cancelEvent`. Facade methods: `calendar_service.reschedule_event_occurrence(...)`, `calendar_service.cancel_event_occurrence(...)`.

### Phase 1 — `rescheduleCalendarEvent` Public GraphQL mutation ✅
- **Model used:** sonnet (plan tier 3). **Branch:** `plan/owner-scoped-reschedule-cancel-mutations/phase-1` (stacked on phase-0b).
- **Commits:** `e9b9167` (feat mutation), `3ccb67d` (test: preservation + non-recurring guard, docstring).
- **What shipped:** `rescheduleCalendarEvent` mutation on the `Mutation` class in `public_api/mutations.py` + `RescheduleCalendarEventInput` (organization_id, calendar_id, event_id, start/end/timezone, optional rrule_string, optional recurrence_id). Decorated `[IsAuthenticated, OrganizationResourceAccess]`, returns `CalendarEventGraphQLType`, raises `GraphQLError`. Owner-scope guard mirrors `schedule_event` (`assert_calendar_in_owner_scope` + Calendar load → `"Calendar not found."` parity). Event load → `"Event not found."`. `recurrence_id` set → `reschedule_event_occurrence` (Phase 0b); else whole/series via `update_event` preserving title/description/attendances/external/resources, with **rrule preservation** (`input.rrule_string` → existing `recurrence_rule.to_rrule_string()` → None). `FIELD_TO_RESOURCE_MAPPING["rescheduleCalendarEvent"] = CALENDAR_EVENT` (already provider-scoped).
- **Tests:** `public_api/tests/test_mutations.py::TestScopedTokenRescheduleCalendarEvent` (10) — whole-event success, field/attendee/resource/description preservation, series rule preserved (no rrule) + replaced (explicit rrule), single-occurrence creates exception/master untouched, non-recurring+recurrenceId clean error, cross-owner not-found parity (no mutation), org-wide acts org-wide, missing-event.
- **Review:** Layer 1/2/3 clean. No BLOCKERs; reviewer verified `to_rrule_string`/`from_rrule_string` symmetry + `update_event` preserves rule id (no series strip). SHOULD-FIX (preservation asserted title-only; non-recurring guard untested) applied; NITs (naive-dt factory, redundant prefetch) skipped as pre-existing/harmless.
- **Outer gate:** serial scoped + Phase 1 tests green; full `pytest -n auto` → **3171 passed**.
- **Env note:** main checkout's `package.json`/`package-lock.json` show a `vinta-ai-workflows ^0.2.0-alpha1 → alpha3` tooling bump that appeared mid-session (unrelated to this feature; NOT a worktree stray write — worktree tree is clean). Excluded from stray-write checks for remaining phases.

### Phase 2 — `rescheduleCalendarGroupEvent` Public GraphQL mutation ✅
- **Model used:** sonnet (plan tier 3). **Branch:** `plan/owner-scoped-reschedule-cancel-mutations/phase-2` (stacked on phase-1).
- **Commits:** `38cc93b` (feat mutation), `274a760` (fix: close existence leak — uniform not-found).
- **What shipped:** `rescheduleCalendarGroupEvent` mutation + `RescheduleCalendarGroupEventInput` (organization_id, event_id, start/end/timezone). Loads the grouped event org-scoped, derives the primary calendar, owner-scope-checks it, wires `group_deps.calendar_group_service.calendar_service = <initialized calendar_service>` + `initialize(org)`, delegates to `reschedule_grouped_event` (moves primary event + linked `group-event-*` BlockedTimes). Whole-event only (group events not recurring in v1). `FIELD_TO_RESOURCE_MAPPING["rescheduleCalendarGroupEvent"] = CALENDAR_EVENT`.
- **SECURITY (BLOCKER fixed):** Because the input is **event-addressed** (no calendar_id), the original two-message split (cross-owner → "Calendar not found.", missing → "Event not found.") was an existence-leak oracle: a scoped token could distinguish "a grouped event I don't own exists" from "doesn't exist". **Fixed to uniform `"Event not found."`** across ALL of: missing event, non-grouped event, cross-owner owner-scope-guard failure, AND the service-layer race (`PermissionDenied("Calendar matching query does not exist.")` sentinel → "Event not found."). NOTE: the **plan body's** API Design for this phase prescribed "Calendar not found." for the calendar-scope failure — that prescription was written for Phase 1's calendar-addressed shape and is SUPERSEDED here by the no-existence-leak invariant in Guiding Decisions. (Phase 1's `"Calendar not found."` stays correct — Phase 1 takes calendar_id as the addressing key.)
- **Tests:** `TestScopedTokenRescheduleCalendarGroupEvent` (6) — success (primary event + linked BlockedTimes move), cross-owner uniform not-found (== missing message, no mutation), org-wide acts org-wide, non-grouped → "Event not found.", PermissionDenied-sentinel → "Event not found." (defense-in-depth), service `CalendarGroupValidationError` → clean GraphQLError (no 500).
- **Review:** Layer 1/2/3. Reviewer flagged the existence leak as BLOCKER (confirmed); fixed. NITs (redundant Calendar.get dropped; organization_id-ignored comment) applied.
- **Outer gate:** serial 6 passed; full `pytest -n auto` → **3177 passed**.
- **DI note for Phase 3:** `get_calendar_mutation_dependencies()` returns FRESH `Factory` instances; wire the initialized `calendar_service` into the group service before `initialize(org)` (mirror the `*WithCode` group wiring). `cancel_grouped_event(event_id, delete_series=…)` is the group cancel path.

### Phase 3 — `cancelEvent` Public GraphQL mutation ✅
- **Model used:** sonnet (plan tier 3). **Branch:** `plan/owner-scoped-reschedule-cancel-mutations/phase-3` (stacked on phase-2).
- **Commits:** `4ae6041` (feat mutation), `04a4bde` (test: strengthen recurring + scope coverage).
- **What shipped:** `cancelEvent` mutation + `CancelEventResult{success}` + `CancelEventInput{organization_id, calendar_id, event_id, delete_series=False, recurrence_id?}`. Calendar-addressed; owner-scope guard on `calendar_id` (cross-owner → `"Calendar not found."`, leak-safe — correct for a calendar-addressed input). Event load by `(event_id, calendar_fk_id=calendar_id)` → `"Event not found."`. Three-way branch: `recurrence_id` → `cancel_event_occurrence` (Phase 0b); grouped (`calendar_group_fk_id`) → `cancel_grouped_event(event_id, delete_series)` (wired group service, deletes primary + linked BlockedTimes); else → `delete_event(calendar_id, event_id, delete_series)`. Returns `CancelEventResult(success=True)`. `FIELD_TO_RESOURCE_MAPPING["cancelEvent"] = CALENDAR_EVENT`.
- **Recurring footgun (documented + pinned):** recurring master + `deleteSeries=false` + no `recurrenceId` deletes the master row (not a series wipe, not a no-op). Test pins it.
- **Tests:** `TestScopedTokenCancelEvent` (10) — single-event success, series delete (with materialized instance + exception, all gone + rule gone), single-occurrence cancel (exception created AND occurrence omitted from `get_occurrences_in_range`), grouped cancel (primary + BlockedTimes gone), cross-owner not-found (no deletion), org-wide acts org-wide, recurring-master footgun, event-not-found, event-on-different-owned-calendar → "Event not found.", missing-grant denied.
- **Review:** Layer 1/2/3. No BLOCKERs; existence-leak verdict clean (calendar-addressed). Reviewer caught that the original single-occurrence test cancelled a non-existent occurrence date (master on a Friday vs `BYDAY=TH`); strengthened to a real occurrence.
- **Outer gate:** serial 10 passed; full `pytest -n auto` → **3187 passed**.

## DEFERRED FOLLOW-UP (out of this plan's scope)
- **Orphaned `RecurrenceRule` on master delete with `delete_series=False`.** In `CalendarEventService.delete_event`, deleting a recurring MASTER with `delete_series=False` (the documented footgun path `cancelEvent` now exposes) cascade-deletes child instances + exceptions but NOT the `RecurrenceRule` (the OneToOne points master→rule, so it doesn't cascade) — leaving a dangling rule row. This is **pre-existing** service behavior affecting all delete callers (REST destroy, token destroy, `cancelEventWithCode`), not introduced by this plan. Deliberately NOT fixed here to avoid late shared-service changes in a GraphQL phase. Recommended follow-up: in `delete_event`'s recurring-master fall-through, delete `event.recurrence_rule` (mirror the `delete_series=True` branch's rule cleanup), with a service-level regression test. Small + strictly-better (no orphans).

## Status: ALL PHASES COMPLETE
0a ✅ · 0b ✅ · 1 ✅ · 2 ✅ · 3 ✅. No cross-repo phases, no flag-removal phase (no feature flag).
