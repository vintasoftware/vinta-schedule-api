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

## Current phase
- **Phase 2 — Add the `ExternalEventChangeRequest` model** (Tier 2 → sonnet, migration-author) — next

## Remaining phases
- Phase 3 — Intercept inbound UPDATES into change requests (Tier 3 → sonnet)
- Phase 4 — Intercept inbound DELETIONS into change requests (Tier 3 → sonnet)
- Phase 5a — Approve a change request (Tier 3 → sonnet)
- Phase 5b — Reject a change request / outbound undo (Tier 4 → opus)
- Phase 6 — FORBIDDEN mode auto-undo during sync (Tier 3 → sonnet)
- Phase 7 — Notify eligible approvers on request creation (Tier 2 → sonnet)
- Phase 8a — REST endpoints (Tier 3 → sonnet)
- Phase 8b — Public GraphQL query + mutations (Tier 3 → sonnet)

## Deferred phases
_(none — no cross-repo or flag-removal phases in this plan)_
