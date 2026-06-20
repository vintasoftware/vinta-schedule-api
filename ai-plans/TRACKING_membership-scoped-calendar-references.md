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

### Phase 1 — CalendarOwnership: expand + backfill ✅
- **Status**: merged-ready (PR open)
- **Model used**: claude-sonnet-4-6 (plan tier: T3) · migration-author + reviewer + fixer
- **Branch**: `plan/membership-scoped-calendar-references/phase-1` (stacked on phase-0)
- **Base**: `plan/membership-scoped-calendar-references/phase-0`
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/147
- **Summary**: Added `membership = OrganizationMembershipForeignKey(PROTECT, related_name="calendar_ownerships",
  null=True)` to `CalendarOwnership` alongside the kept `user` FK; composite index
  `(organization, membership_user_id)` (`calownership_org_member_idx`). Migration 0022 (schema: nullable
  column + index), 0023 (data, `atomic=False`: batched idempotent/resumable `UPDATE … WHERE … EXISTS(membership)`;
  orphans → NULL + CSV to `.vinta-ai-workflows/one-off-runs/` + WARNING; clean reverse). Behaviour-preserving
  (user FK + M2M untouched, no read/write/API change). Reviewer SHOULD-FIX fixed: `atomic=False`, tested the
  CSV/OSError/reverse paths, documented the cross-org raw-SQL exception. 12 tests.
- **Gate**: `pytest -n auto` → 2631 passed; migrate forward/reverse/forward clean; `makemigrations --check`
  no changes; `check --deploy` clean.
- **Carry-forward**: `related_name="calendar_ownerships"` is shared by `user`→User and `membership`→OrganizationMembership
  (no clash, different targets); Phase 2 drops `user` and hands the name to `membership`. Real PROTECT FK
  (raw-SQL composite) lands in Phase 2 cutover.

### Phase 2a — CalendarOwnership cutover (app layer) ✅
- **Status**: merged-ready (PR open). **Note**: plan Phase 2 was split into 2a (app layer) + 2b (migration) per the plan's sanctioned split.
- **Model used**: claude-opus (plan tier: T4) · implementer + reviewer + fixer
- **Branch**: `plan/membership-scoped-calendar-references/phase-2a` (stacked on phase-1)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/148
- **Summary**: Routed all `CalendarOwnership.user` reads/writes through `membership`/`membership_user_id`;
  replaced `Calendar.users` M2M with `Calendar.memberships` (state-only migration 0024); exposed owner as
  membership `{user_id, organization_id, role}` in GraphQL + REST. `user` column KEPT (2b drops it). M2M-over-
  ForeignObject de-risked by spike (two-hop JOIN compiles). Orphans excluded from membership reads; orphan-only
  `admin_sync`/`transfer` → 400 (tested). Reviewer BLOCKER fixed (stale-membership crash in write-adapter →
  resolve user via scalar `membership_user_id`); sync `update_or_create` kept keying on `user` for the dual-column
  window. Security scoping verified equal-or-stricter.
- **Gate**: `pytest -n auto` → 2642 passed; `makemigrations --check` clean; `check --deploy` clean.
- **Carry-forward for 2b**: drop `user` FK + reverse accessor; rewrite `grant_calendar_owner_permissions`
  (still traverses `user.organization_memberships.calendar_ownerships`); re-key sync `update_or_create` to
  `membership_user_id` + add a partial unique index `(calendar_fk, membership_user_id) WHERE membership_user_id
  IS NOT NULL`; add raw-SQL composite PROTECT FK `(organization_id, membership_user_id) → organization_membership
  (user_id, organization_id) ON DELETE RESTRICT` (NOT VALID then VALIDATE). Note: once the FK exists, a non-null
  `membership_user_id` MUST reference an existing membership.

## Current phase
- Phase 2b — CalendarOwnership cutover (migration: drop user + PROTECT FK) (next)

## Remaining phases
- Phase 2b — CalendarOwnership cutover migration (T4 · migration-author)
- Phase 3 — EventAttendance expand + backfill (T3 · migration-author)
- Phase 4 — EventAttendance cutover (T4 · implementer)
- Phase 5 — CalendarManagementToken expand + backfill (T2 · migration-author)
- Phase 6 — CalendarManagementToken cutover (T4 · implementer)
- Phase 7a — FK conversions + `.id` rewrites (T4 · implementer)
- Phase 7b — OrganizationMembership composite PK (T4 · migration-author)

## Deferred phases
- None (no cross-repo, no flag-removal phases in this plan).
