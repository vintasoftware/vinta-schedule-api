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
| 0b | Single-occurrence exception service methods | 4 | ⏳ next |
| 1 | `rescheduleCalendarEvent` mutation | 3 | ⬜ pending |
| 2 | `rescheduleCalendarGroupEvent` mutation | 3 | ⬜ pending |
| 3 | `cancelEvent` mutation | 3 | ⬜ pending |

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

## Current Phase
**Phase 0b** — Single-occurrence exception service methods (`reschedule_event_occurrence`, `cancel_event_occurrence`).

## Remaining Phases
0b (next), 1, 2, 3.
