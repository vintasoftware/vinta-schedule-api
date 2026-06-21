# Tracking — External Event Update Policy

- **Plan**: `ai-plans/2026-06-21-EXTERNAL_EVENT_UPDATE_POLICY_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-21
- **Last updated**: 2026-06-21
- **Feature flag**: none (the `external_event_update_policy` field is permanent product config; default `CHANGE_REQUEST`)

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `worktree_path`: `.claude/worktrees/plan-external-event-update-policy`
- `worktree_branch`: `plan/external-event-update-policy`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-external-event-update-policy.yaml`
- `commit_strategy_resolved`: modular-commits
- `plan_branch`: `plan/external-event-update-policy`
- `pr_url`: (pending — opens after Phase 1)

## Completed phases
- **Phase 1 — Add the `external_event_update_policy` field** ✅
  - Model: haiku (Tier 1), agent: implementer + reviewer (sonnet) + fixer (haiku)
  - Commits: `feat(organizations): add external_event_update_policy field`, `chore(organizations): expose external_event_update_policy in admin`, `fix(organizations): harden external_event_update_policy field + tests`, `refine(organizations): restore precise admin docstring …`
  - Added `ExternalEventUpdatePolicy` TextChoices (`allow`/`change_request`/`forbidden`) + field on `Organization` with `default`/`db_default=CHANGE_REQUEST`; migration `0014`; admin list_display/list_filter/fieldset; unit tests (default, choices, settable to each with `refresh_from_db`).
  - Review: 0 BLOCKERs; SHOULD-FIX (db_default, refresh_from_db round-trip, change_request test) + NIT (admin docstring) all applied.
  - Outer gate green: `check --deploy` clean (5 expected dev warnings), full `pytest -n auto` = 2971 passed.

- **Phase 2 — Add the `ExternalEventChangeRequest` model** ✅
  - Model: sonnet (Tier 2), agent: migration-author + reviewer (sonnet) + fixer (migration-author, sonnet)
  - Commits: `feat(calendar): add ExternalEventChangeRequest model`, `chore(calendar): register ExternalEventChangeRequest in admin`, `fix(calendar): harden ExternalEventChangeRequest model per review`
  - Model `ExternalEventChangeRequest(OrganizationModel)` + `ExternalEventChangeKind`/`ExternalEventChangeRequestStatus` (in `constants.py`); `event` FK `SET_NULL`+nullable (preserves request history when Phase 5a deletes the event); partial unique constraint `(event_fk, organization) WHERE status=pending`; composite resolver index; manager/queryset/admin/factory; migration `0037` + raw-SQL composite PROTECT FK `0038` (deferrable, NOT VALID+VALIDATE). 10 model tests.
  - Review: 2 BLOCKERs (PROTECT→SET_NULL; late import → enums to constants) + 6 SHOULD-FIX (org in unique key, mandated resolver index, raw-SQL composite FK, manager/queryset/for_event, missing tests, factory org guard) all applied. Reviewer's `from_queryset` suggestion correctly declined (siblings use explicit `get_queryset`).
  - Verified: migrations reverse+reapply cleanly; `makemigrations --check` clean; full `pytest -n auto` = 2981 passed.
  - ⚠️ Phase 5a note: `event` is now nullable (`SET_NULL`). Approving a `delete`-kind request deletes the CalendarEvent; the resolved request row survives with `event=NULL`.

- **Phase 3 — Intercept inbound UPDATES into change requests** ✅
  - Model: sonnet (Tier 3), agent: implementer + reviewer + fixer (sonnet)
  - Commits: `feat(calendar): add ExternalEventChangeRequestService for inbound update requests`, `feat(calendar): divert inbound external updates to change requests under change_request policy`, `fix(calendar): fail loud on missing change-request service + derive provider + typing`
  - New `ExternalEventChangeRequestService.create_or_supersede_update_request` (atomic supersede: prior PENDING→STALE, new PENDING, audit via SYSTEM actor); `audit/constants.py` gained all 4 `EXTERNAL_CHANGE_*` actions. `_process_existing_event` reads policy: ALLOW=direct-apply (byte-for-byte), CHANGE_REQUEST=divert + `matched_event_ids` (raises `ImproperlyConfigured` if service missing), FORBIDDEN=falls through to ALLOW (Phase 6 TODO). Service is optional `__init__` param threaded via facade `_get_sync_service()` + DI; provider derived from `context.calendar_adapter.provider`.
  - Review: 1 BLOCKER (silent None-service fallback → fail loud) + SHOULD-FIX (provider, typing) all applied; BLOCKER guard test added.
  - Verified: full `pytest -n auto` = 2987 passed; mypy 0 new errors; `makemigrations --check` clean.

- **Phase 4 — Intercept inbound DELETIONS into change requests** ✅
  - Model: sonnet (Tier 3), agent: implementer + reviewer + fixer (haiku)
  - Commits: `feat(calendar): add create_or_supersede_delete_request…`, `feat(calendar): divert inbound external deletions to change requests…`, `test(calendar): add Phase 4 deletion-interception tests…`, `fix(calendar): polish delete-request audit diff + tests per review`
  - `create_or_supersede_delete_request` (kind=DELETE, empty proposed_values, `retained_values` for Phase 5b re-create) + shared `_supersede_pending` helper. Cancelled-event branch mirrors the UPDATE branch: ALLOW=direct-delete, CHANGE_REQUEST=divert (fail-loud if service None)+`matched_event_ids`, FORBIDDEN→ALLOW (Phase 6 TODO). Updated pre-existing `test_process_existing_event_cancelled` to set ALLOW.
  - Review: 0 BLOCKERs; SHOULD-FIX (audit-diff include all retained fields, stronger test assertions) + NITs (annotation parity, comment accuracy) applied.
  - Verified: full `pytest -n auto` = 2993 passed (re-ran after fixer reported a transient "108 collection errors" — confirmed false; collect-only = 2993 clean); `makemigrations --check` clean.

- **Phase 5a — Approve a change request** ✅
  - Model: sonnet (Tier 3), agent: implementer + reviewer + fixer (sonnet)
  - Commits: `feat(calendar): add change-request eligibility + approve to ExternalEventChangeRequestService`, `test(calendar): add cross-timezone approval test + fix comment/imports per review`
  - `can_resolve(request, membership)` (admin OR member-attendee, org-scoped) + `approve(request, *, membership)`: UPDATE applies `proposed_values` to the local event's `start_time_tz_unaware`/`end_time_tz_unaware` (GeneratedField base columns; `.replace(tzinfo=None)` keeps event-local wall-clock — **cross-tz round-trip proven by test**), DELETE deletes the CalendarEvent (request survives `event=NULL` via SET_NULL). New domain exceptions `ChangeRequestNotPendingError` (→409) / `ChangeRequestIneligibleError` (→403, PermissionDenied). Audit `EXTERNAL_CHANGE_APPROVED`.
  - Review: 0 BLOCKERs; reviewer's "co-author trailers" + tz-BLOCKER claims both verified FALSE (no trailers; round-trip correct). SHOULD-FIX (cross-tz test, comment, tenant-scope, hoist imports) applied.
  - Verified: full `pytest -n auto` = 3004 passed; `makemigrations --check` clean; no trailers.
  - 🔑 `can_resolve` is the shared eligibility helper for Phase 5b + Phase 8 API.

- **Phase 5b — Reject a change request / outbound undo** ✅
  - Model: opus (Tier 4), agent: implementer + reviewer (opus) + fixer (opus)
  - Commits: `feat(calendar): add reject + outbound undo to ExternalEventChangeRequestService`, `fix(calendar): hydrate attendees/recurrence + de-orphan provider create in reject undo`
  - `reject(request, *, membership, write_adapter)` + reusable `_undo_on_provider` + `_build_adapter_input(event)`. UPDATE→`update_event` re-converges GCal to retained; DELETE→`create_event` re-creates + rebinds local `external_id`. **Auth seam**: authenticated `write_adapter` passed in by caller (no `CalendarService` injection → no import cycle); Phase 6 (sync) + Phase 8 (API) reuse `_undo_on_provider`.
  - Review (opus): id-semantics → reject CORRECTLY uses external ids (existing `CalendarEventService` internal-id usage is a separate latent bug). **2 BLOCKERs fixed**: (C) undo was wiping attendees/recurrence via full-replace PUT → now `_build_adapter_input` hydrates attendees+external attendees+resources+recurrence via shared serialization utils; (B) provider `create_event` was inside `transaction.atomic()` (orphan/duplicate risk) → now provider call is OUTSIDE the txn, short atomic rebinds id+status, compensating `delete_event` on local failure then re-raise. SHOULD-FIX (event-None error type) + NITs applied.
  - Verified: provider-call-outside-txn + compensation confirmed by reading the code; full `pytest -n auto` = 3014 passed; no import cycle; `makemigrations --check` clean; no trailers.
  - ⚠️ Pre-existing latent bug noted (out of scope): `CalendarEventService.update_event` passes INTERNAL ids to the adapter where external ids are needed — masked by mocked tests.

- **Phase 6 — FORBIDDEN mode auto-undo during sync** ✅
  - Model: sonnet (Tier 3), agent: implementer + reviewer + fixer (sonnet)
  - Commits: `refactor(calendar): extract shared resolve-with-undo orchestration`, `feat(calendar): auto-undo inbound external changes under FORBIDDEN policy`, `fix(calendar): make FORBIDDEN auto-undo atomic (single txn + provider-first + compensation)`
  - Extracted shared `_resolve_with_undo` (reused by reject + auto-undo); `auto_undo_inbound_change` creates an `AUTO_UNDONE` row + runs the undo with SYSTEM actor. Sync FORBIDDEN branches (UPDATE+DELETE) wired to call it with `context.calendar_adapter`; fail-loud `ImproperlyConfigured` on service-None OR adapter-None; `matched_event_ids` kept; local event not mutated/deleted.
  - Review: 0→1 BLOCKER fixed — auto-undo atomicity: row creation + supersede were in a SEPARATE txn from the rebind/status (dangling AUTO_UNDONE + destroyed PENDING on provider failure). Fixed via `prepare_fn` closure run INSIDE the single atomic block AFTER the provider call; provider-failure now commits nothing. SHOULD-FIX (dead-code elif/else removed; 2 tests added: supersede→STALE, provider-raises→no row) applied. Reject's 10 tests pass unchanged.
  - Verified: full `pytest -n auto` = 3029 passed; no new mypy; no import cycle; `makemigrations --check` clean; no trailers.

- **Phase 7 — Notify eligible approvers on request creation** ✅
  - Model: sonnet (Tier 2), agent: implementer + reviewer + fixer (haiku)
  - Commits: `feat(calendar): notify eligible approvers in-app on change-request creation`, `fix(calendar): scope approver notifications to active members + avoid extra query`
  - `_notify_eligible_approvers(request, event)` dispatches in-app (`NotificationService.create_notification`, `NotificationTypes.IN_APP`) to ACTIVE member-attendees ∪ active org admins (set-dedup), wrapped in `transaction.on_commit`. Called from both `create_or_supersede_*`; NOT from `auto_undo_inbound_change` (FORBIDDEN). New `notification_contexts.py` + template + `apps.py ready()` + DI (`notification_service`). `notification_service` Optional (graceful skip if None — observability, not safety).
  - Review: 0 BLOCKERs; SHOULD-FIX (attendee query + `can_resolve` now require active membership; pass `event` to avoid extra query; inactive-admin test) + NITs applied.
  - Verified: full `pytest -n auto` = 3041 passed; `makemigrations --check` clean; no trailers.

- **Phase 8a — REST endpoints** ✅
  - Model: sonnet (Tier 3), agent: implementer + reviewer + fixer (sonnet)
  - Commits: `feat(change-requests): add REST endpoints for listing, approving, and rejecting ExternalEventChangeRequests`, `fix(calendar): correct reject status codes + ordering + prune virtual model per review`
  - `ExternalEventChangeRequestViewSet` (list/approve/reject) + serializer + filterset + permission + `resolvable_by(membership)` queryset/manager (admin→all org; member→their attended events) + route + regenerated `schema.yml`. reject authenticates outbound via CalendarOwnership→owner SocialAccount→`authenticate`→`_get_write_adapter_for_calendar` (mirrors `CalendarEventViewSet.transfer`). Guard ordering: 409 non-PENDING (before auth) → 403 event-None → 400 missing owner/account/adapter → 403 service-ineligible → 200. Default PENDING filter scoped to `list` only.
  - Review: 0 BLOCKERs; SHOULD-FIX (reject 409→403 for event-None + reorder so non-PENDING returns 409 before auth work; prune unused virtual-model traversals; +4 error-path tests + field assertions) applied.
  - Verified: full `pytest -n auto` = 3065 passed; `makemigrations --check` clean; schema validated; no trailers.

- **Phase 8b — Public GraphQL query + mutations** ✅
  - Model: sonnet (Tier 3), agent: implementer + reviewer + fixer (sonnet)
  - Commits: `feat(public-api): add GraphQL query + mutations for ExternalEventChangeRequest`, `fix(public-api): fix resolvedBy resolver + add cross-org/pagination validation + tests`
  - Strawberry type + `externalEventChangeRequests` query (scoped token → `resolvable_by`; org-wide token → all org) + `approve`/`reject` mutations (mutations require a scoped/acting membership; reject authenticates outbound like REST). `FIELD_TO_RESOURCE_MAPPING` (3 fields) + `PublicAPIResources.EXTERNAL_EVENT_CHANGE_REQUEST`.
  - Review: 1 BLOCKER fixed — `resolvedBy` resolver accessed `self.resolved_by` self-referentially (RecursionError on any query selecting it; untested) → fixed via `resolved_by_user_id` null-guard + `object.__getattribute__`. SHOULD-FIX (cross-org isolation tests for both mutations; offset/limit/status validation; reject N+1 → pass resolved `org`) applied.
  - Verified: full `pytest -n auto` = 3093 passed; `makemigrations --check` clean; schema unchanged; no trailers.

## 🎉 ALL PHASES COMPLETE
All 10 executable phases (1, 2, 3, 4, 5a, 5b, 6, 7, 8a, 8b) implemented, reviewed (adversarial reviewer + fixer loop each), and verified. No cross-repo or flag-removal phases. Branch `plan/external-event-update-policy` (36 commits) pushed to origin. Full suite green at 3093 passed.

## Deferred phases
_(none — no cross-repo or flag-removal phases in this plan)_
