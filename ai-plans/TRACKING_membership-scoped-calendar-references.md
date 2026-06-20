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

### Phase 2b — CalendarOwnership cutover (migration) ✅
- **Status**: merged-ready (PR open). **Model**: claude-opus (T4) · migration-author + reviewer + fixer.
- **Branch**: `plan/membership-scoped-calendar-references/phase-2b` (stacked on 2a) · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/149
- **Summary**: Dropped `CalendarOwnership.user` (0025, `SeparateDatabaseAndState` so reverse re-adds `user_id`
  nullable + backfills); partial unique `(calendar_fk, membership_user_id) WHERE NOT NULL`; raw-SQL composite
  PROTECT FK (0026, `atomic=False`, NOT VALID/VALIDATE). Reviewer BLOCKER: FK was non-deferrable RESTRICT →
  changed to **`NO ACTION DEFERRABLE INITIALLY DEFERRED`** (org-cascade deletes membership+ownership in one txn;
  deferred check passes at commit, membership-only delete still raises). Deeper fix: the `membership` ForeignObject
  must be **`on_delete=DO_NOTHING`** (PROTECT carried by it made Django's collector raise `ProtectedError` eagerly
  on cascade) — changed in `common/fields.py` (0027, state-only). Owner-resolution writes guarded by
  `_resolve_owner_membership_user_id` (FK rejects non-member ids). 2640 passed.

## ⚠️ CARRY-FORWARD for Phases 3–6 (EventAttendance, CalendarManagementToken) — established pattern
1. **Field type is now `DO_NOTHING`**: `OrganizationMembershipForeignKey`'s ForeignObject uses
   `on_delete=DO_NOTHING`; PROTECT is enforced ONLY by the per-table raw-SQL deferred FK. Do not re-add PROTECT
   to the ForeignObject.
2. **Expand phase (3, 5)**: add `membership = OrganizationMembershipForeignKey(null=True, ...)` alongside `user`;
   composite index `(organization, membership_user_id)`; batched `atomic=False` backfill `UPDATE … WHERE
   membership_user_id IS NULL AND EXISTS(membership)`; orphans → NULL + CSV report; mark cross-org raw SQL as a
   sanctioned tenant-scope exception. NOTE: `EventAttendance.user` and `CalendarManagementToken.user` are BOTH
   nullable (null-user rows stay null-membership).
3. **Cutover phase (4, 6)**: rewrite app reads/writes through membership; for EventAttendance also redefine
   `CalendarEvent.attendees` M2M → `attendee_memberships` (M2M-over-ForeignObject is proven viable);
   `update_or_create`/create paths guard membership existence; drop `user` via `SeparateDatabaseAndState`
   (nullable+backfill reverse); add partial unique where appropriate; raw-SQL composite FK
   `(membership_user_id, organization_id) REFERENCES organizations_organizationmembership (user_id, organization_id)
   ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED` (NOT VALID then VALIDATE, atomic=False).
4. **FK column order** (plan prose is wrong): `(membership_user_id, organization_id) → (user_id, organization_id)`.
5. API: expose membership `{user_id, organization_id, role}` (no scalar id until Phase 7).

### Phase 3 — EventAttendance: expand + backfill ✅
- **Status**: merged-ready (PR open). **Model**: claude-sonnet (T3) · migration-author + reviewer (+ orchestrator NIT fixes).
- **Branch**: `plan/membership-scoped-calendar-references/phase-3` (stacked on 2b) · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/150
- **Summary**: Mirror of Phase 1 for EventAttendance — `membership` + `membership_user_id` + index
  `evattend_org_member_idx`; migrations 0028 (schema) + 0029 (backfill, `atomic=False`, batched, orphan CSV,
  reverse). Reviewer found NO blocker/should-fix (faithful pattern replication); 2 cosmetic docstring NITs fixed.
  **KEY FINDING**: `EventAttendance.user_id` is `NOT NULL` in DB (sync code references `user=None` — latent
  inconsistency, untouched). 15 tests. 2655 passed.

## ⚠️ Phase 4b DATA-LOSS CHECKPOINT (Open Question #3)
EventAttendance.user is NOT NULL but membership_user_id is NULL for non-member attendees. Dropping the `user`
column (4b) loses attendee identity for orphan attendances. **Before 4b drops the column, confirm with the user**
(or keep `user` nullable on EventAttendance as a fallback). 4a keeps the column (reversible, no data loss).

### Phase 4a — EventAttendance cutover (app layer) ✅
- **Status**: merged-ready (PR open). **Model**: claude-opus (T4) · implementer + reviewer + fixer.
- **Branch**: `plan/membership-scoped-calendar-references/phase-4a` (stacked on 3) · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/151
- **Summary**: Routed EventAttendance reads/writes through membership; `CalendarEvent.attendees` → `attendee_memberships`
  M2M (state-only 0030); attendee API exposes membership `{user_id, organization_id, role}`; serialize resolves via
  `membership_user_id` (orphan→None, dropped). `resolve_member_user_ids` bulk guard. Reviewer BLOCKER: permission-diff
  asymmetry (serialize_event dropped orphans, serialize_event_data_input didn't → spurious UPDATE_ATTENDEES/PermissionDenied)
  → fixed by filtering input side through the same guard; batched N+1 user lookups. `user` column KEPT. 2668 passed.
- **Recurring non-gating note**: django-stubs can't see `membership_user_id` (the field's contributed column) → ~10 mypy
  `attr-defined` false positives accumulating across phases. Candidate Phase-0 typing cleanup (add the attr to the field's
  type surface). Not gating (mypy not in CI).
- **Carry-forward for 4b** (remaining `EventAttendance.user`/`user_id` non-test refs to retarget when dropping the column):
  `calendar_sync_service.py` (`attendances.filter(user_id=…)`/`filter(user=user)`), `calendar_event_service.py`
  (`user_id__in=attendances_to_delete`, `__str__` on models), `mutations.py`, `calendar_bundle_service.py`,
  `calendar_group_service.py`, `serialize_event_data_input` `a.user_id` map. Plus the established 2b migration pattern.

### Phase 4b — EventAttendance cutover (migration) ✅
- **Status**: merged-ready (PR open). **DATA-LOSS DECISION: user chose "Drop user (uniform end-state)"** — orphan
  attendances permanently lose identity (accepted). **Model**: claude-opus (T4) · migration-author + reviewer + fixer.
- **Branch**: `…/phase-4b` (stacked on 4a) · **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/152
- **Summary**: Dropped `EventAttendance.user` (0031 `SeparateDatabaseAndState`, lossy reverse backfill); deferrable
  raw PROTECT FK (0032). NO partial unique (no upsert; dup-row risk). Reviewer BLOCKER: sync created duplicate orphan
  attendances every sync for non-member resolved Users (no dedupe key post-drop) → fixed by routing non-members to the
  external-attendee path (deduped by email; internal attendance == membership). 2662 passed. Deleted test_attendance_expand.py
  (mirrors 2b deleting test_ownership_expand.py).

## Current phase
- Phase 5 — CalendarManagementToken expand + backfill (next). NOTE: `CalendarManagementToken.user` is genuinely
  NULLABLE (the `external_attendee` alternative). Backfill member rows; null-user (external) tokens stay NULL and are
  NOT orphans; user-set-but-non-member → NULL + reported. Mirror Phase 3.

## Remaining phases
- Phase 5 — CalendarManagementToken expand + backfill (T2 · migration-author)
- Phase 6 — CalendarManagementToken cutover (T4 · implementer/migration-author) [likely split 6a/6b]
- Phase 7a — FK conversions (OrganizationInvitation.membership + public_api.SystemUser.scoped_to_membership) + .id rewrites (T4)
- Phase 7b — OrganizationMembership composite PK (T4 · migration-author)
- Phase 3 — EventAttendance expand + backfill (T3 · migration-author)
- Phase 4 — EventAttendance cutover (T4 · implementer)
- Phase 5 — CalendarManagementToken expand + backfill (T2 · migration-author)
- Phase 6 — CalendarManagementToken cutover (T4 · implementer)
- Phase 7a — FK conversions + `.id` rewrites (T4 · implementer)
- Phase 7b — OrganizationMembership composite PK (T4 · migration-author)

## Deferred phases
- None (no cross-repo, no flag-removal phases in this plan).
