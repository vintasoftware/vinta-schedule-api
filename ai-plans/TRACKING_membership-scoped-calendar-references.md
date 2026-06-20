# Tracking — Membership-Scoped Calendar References

- **Plan**: `ai-plans/2026-06-19-MEMBERSHIP_SCOPED_CALENDAR_REFERENCES_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-19
- **Last updated**: 2026-06-19

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
- `commit_strategy_resolved`: stacked-branches
- `worktree_path`: `.claude/worktrees/plan-membership-scoped-calendar-references`
- `worktree_branch`: `plan/membership-scoped-calendar-references/wt`
- `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-membership-scoped-calendar-references.yaml`
- PR policy: agents-create · inline comments via `gh api` (open-pr.sh short-circuits on already-published)

## Completed phases

### Phase 0 — Add OrganizationMembershipForeignKey field type ✅
- **Status**: merged-ready (PR open)
- **Model used**: claude-sonnet-4-6 (plan tier: T3) · implementer + reviewer + fixer
- **Branch**: `plan/membership-scoped-calendar-references/phase-0`
- **Base**: `main`
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/146
- **Summary**: Added `OrganizationMembershipForeignKey` to `common/fields.py`, modelled on
  `TenantSafeForeignKey`. Contributes a concrete `<name>_user_id` `BigIntegerField` + a non-editable
  `ForeignObject` joining `(organization_id, <name>_user_id)` → `OrganizationMembership(organization_id,
  user_id)`. No model adopts it yet; no migration generated (`makemigrations --check` clean). Reviewer
  raised two SHOULD-FIX: (1) test only string-inspected `from_fields`/`to_fields` → fixer added a
  DB-backed behavioral test (schema_editor-created table; asserts descriptor resolution,
  `select_related` = 1 query, `filter(membership__role=…)` traversal); (2) join column unindexed →
  documented on the field that adopters declare the tenant-leading composite index
  `(organization_id, <name>_user_id)` per table (NOT baked in, to avoid a wasted single-col index).
  Orchestrator empirically confirmed the JOIN SQL against the real model.
- **Gate**: `pytest -n auto` → 2619 passed; `check --deploy` clean; `makemigrations --check` no changes.
- **Carry-forward for Phase 1+**: each expand migration (Phases 1/3/5) MUST add the composite index
  `(organization_id, <name>_user_id)`; each cutover (2/4/6) adds the raw-SQL composite FK
  `… REFERENCES organization_membership(user_id, organization_id) ON DELETE RESTRICT` for PROTECT.

## Current phase
- Phase 1 — CalendarOwnership: expand + backfill (next)

## Remaining phases
- Phase 1 — CalendarOwnership expand + backfill (T3 · migration-author)
- Phase 2 — CalendarOwnership cutover (T4 · implementer)
- Phase 3 — EventAttendance expand + backfill (T3 · migration-author)
- Phase 4 — EventAttendance cutover (T4 · implementer)
- Phase 5 — CalendarManagementToken expand + backfill (T2 · migration-author)
- Phase 6 — CalendarManagementToken cutover (T4 · implementer)
- Phase 7a — FK conversions + `.id` rewrites (T4 · implementer)
- Phase 7b — OrganizationMembership composite PK (T4 · migration-author)

## Deferred phases
- None (no cross-repo, no flag-removal phases in this plan).
