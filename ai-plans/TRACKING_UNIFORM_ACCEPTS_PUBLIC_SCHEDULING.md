# Tracking — UNIFORM_ACCEPTS_PUBLIC_SCHEDULING

- **Feature**: Uniform `accepts_public_scheduling` / `is_private` across Calendar, CalendarGroup & bundles
- **Plan**: `ai-plans/2026-06-18-UNIFORM_ACCEPTS_PUBLIC_SCHEDULING_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-18
- **Last updated**: 2026-06-19

## Run options
- `commit_strategy_resolved`: modular-commits (single branch `plan/uniform-accepts-public-scheduling`, one PR)
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `use_worktree`: true
  - `worktree_path`: `.claude/worktrees/plan-uniform-accepts-public-scheduling`
  - `worktree_branch`: `plan/uniform-accepts-public-scheduling` (base `main`)
  - `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-uniform-accepts-public-scheduling.yaml`
- `plan_branch`: `plan/uniform-accepts-public-scheduling`

## Notes
- No feature flag (project has none). No flag-removal phase.
- No cross-repo phases. All 7 phases executable in-repo.
- Branch rebased onto `main` (`a5e470d`) after Phase 1, then again onto `main` (`c33708f`, PR #141
  per-owner-scoped token writes) after Phase 2 — both at user request ("issues fixed in main").
  After the first rebase removed stray untracked `public_api/migrations/0008_merge_*` +
  `0009_alter_resourceaccess_*` (auto-generated cruft referencing a node main deleted). Second
  rebase auto-merged `graphql.py` + `schema.yml` cleanly; verified by schema regen (no diff) and
  full suite. Branch force-pushed after each rebase.
- PR not yet opened: `gh` / `yq` missing on host → `open-pr.sh` can't run. prs-context written
  `status: pending` at `.vinta-ai-workflows/prs-context/uniform-accepts-public-scheduling/plan.md`.

## Completed phases

### Phase 1 — Add `accepts_public_scheduling` to `CalendarGroup` ✅
- **Model used**: `claude-haiku-4-5` (plan tier: Tier 1). Agent: `migration-author`.
- **Commit**: `feat(calendar): add accepts_public_scheduling to CalendarGroup`
- **Migration**: `calendar_integration/migrations/0021_calendargroup_accepts_public_scheduling.py` (single `AddField`, `default=False`, no backfill).
- **Files**: `calendar_integration/models.py`, the migration, `calendar_integration/tests/test_calendar_group_models.py` (3 tests).
- **Gate**: `check --deploy` clean; `makemigrations --check` clean; full suite **2450 passed**.
- **Review**: Layer 3 reviewer — no BLOCKER/SHOULD-FIX; 2 NITs (both no-change-required).
- **Summary**: Adds the canonical privacy knob to `CalendarGroup` mirroring `Calendar.accepts_public_scheduling`. Default False (private), no backfill — secure-by-default. Column unread this phase (purely additive); read/write surfaces land in later phases.

### Phase 2 — Expose `is_private` (read) on the three GraphQL types ✅
- **Model used**: `claude-haiku-4-5` (plan tier: Tier 2). Agent: `implementer`.
- **Commit**: `feat(calendar): expose is_private on calendar/group/bundle GraphQL types`
- **Files**: `calendar_integration/graphql.py` (3 `is_private` resolvers + bundle docstring), `public_api/tests/test_is_private_field.py` (6 tests).
- **Gate** (post-rebase on `c33708f`): `check --deploy` clean; `makemigrations --check` clean; `schema.yml` regen no-diff; full suite **2528 passed**.
- **Review**: Layer 3 reviewer — no BLOCKER/SHOULD-FIX; 2 NITs (no-action). Rebase integrity verified (my resolvers + main's owner-scope resolvers coexist).
- **Summary**: `is_private = not accepts_public_scheduling` exposed read-only on Calendar/Group/Bundle types. Derived (no stored field). GraphQL schema not snapshotted, so no schema artifact change; REST `schema.yml` unaffected.

### Phase 3 — Accept `is_private` on CalendarGroup create/update inputs ✅
- **Model used**: `claude-haiku-4-5` (plan tier: Tier 2). Agent: `implementer` + `fixer`.
- **Commits**: `feat(calendar): accept is_private on calendar group create/update inputs` (`5a38b69`); `fix(calendar): conditional update_fields for group privacy + strengthen tests` (`dd525da`).
- **Files**: `calendar_integration/mutations.py`, `calendar_integration/services/dataclasses.py` (`CalendarGroupInputData.accepts_public_scheduling`), `calendar_integration/services/calendar_group_service.py`, `calendar_integration/tests/test_calendar_group_graphql.py`.
- **Gate**: `check --deploy` clean; `makemigrations --check` clean; full suite **2534 passed**.
- **Review**: Layer 3 — 1 SHOULD-FIX (privacy field always in `update_fields` → clobber risk), fixed (conditional `update_fields`); 2 NITs fixed (load-bearing default test, redundant comment). Re-reviewed clean.
- **Summary**: `is_private` accepted on create (default True/private) + update (None=unchanged), translated to `accepts_public_scheduling = not is_private`. Note: DRF serializer write path does NOT carry the field (out of scope; secure-by-default holds) — see Open Questions if REST parity is wanted.

### Phase 4 — Accept `is_private` on bundle create/update inputs ✅
- **Model used**: `claude-haiku-4-5` (plan tier: Tier 2). Agent: `implementer` + `fixer`.
- **Commits**: `feat(public_api): accept is_private on calendar bundle create/update inputs` (`c8dc721`); `test(public_api): assert isPrivate round-trip on bundle mutations` (`f8e7c55`).
- **Files**: `public_api/mutations.py`, `calendar_integration/services/calendar_bundle_service.py`, `calendar_integration/services/calendar_service.py`, `public_api/tests/test_mutations.py`.
- **Gate**: `check --deploy` clean; `makemigrations --check` clean; full suite **2541 passed**.
- **Review**: Layer 3 — 1 SHOULD-FIX (tests asserted column only, not GraphQL `isPrivate` round-trip), fixed; symmetric omit-test NIT added. Bundle update resolver already had a conditional `update_fields` idiom (no clobber). Re-reviewed clean.
- **Summary**: `is_private` on bundle create (default private) + update (None=unchanged) → bundle `Calendar.accepts_public_scheduling`. Threaded through `CalendarService` + `CalendarBundleService`. DRF bundle create serializer falls through to private default (out of scope).

### Phase 5 — Accept `is_private` on resource-calendar input ✅
- **Model used**: `claude-haiku-4-5` (plan tier: Tier 2). Agent: `implementer`.
- **Commit**: `feat(public_api): accept is_private on resource calendar create input` (`7d0d905`).
- **Files**: `public_api/mutations.py`, `calendar_integration/services/calendar_service.py`, `public_api/tests/test_mutations.py`.
- **Gate**: `check --deploy` clean; `makemigrations --check` clean; full suite **2543 passed**.
- **Review**: Layer 3 — no BLOCKER/SHOULD-FIX; 2 no-action NITs. Create-only (resource calendars have no update input). Caller threading verified backward-compatible (DRF serializer falls through to private default). Round-trip `isPrivate` asserted.
- **Summary**: `is_private` (default True/private) on `CreateResourceCalendarInput` → `Calendar.accepts_public_scheduling`.

### Phase 6 — New plain-Calendar create/update mutation with `is_private` ✅
- **Model used**: `claude-sonnet-4-6` (plan tier: Tier 3). Agent: `implementer` + `fixer` (sonnet).
- **Commits**: `feat(calendar_service): add create_calendar and update_calendar service methods` (`894861c`); `feat(public_api): add createCalendar/updateCalendar mutations with is_private` (`a601e0a`); `fix(public_api): gate createCalendar/updateCalendar on granular write resources` (`0aca730`).
- **Files**: `calendar_integration/services/calendar_service.py` (`create_calendar`/`update_calendar`, PERSONAL/INTERNAL, org-scoped, PERSONAL-only guard, conditional update), `public_api/mutations.py` (inputs/results/resolvers), `public_api/constants.py` (+`CREATE_CALENDAR`/`UPDATE_CALENDAR`), `public_api/permissions.py` (mapping), `public_api/tests/test_calendar_mutations.py` (new, ~18 tests).
- **Gate**: `check --deploy` clean; `makemigrations --check` clean (class-form choices → no migration needed); full suite **2561 passed**.
- **Review**: Layer 3 — 1 BLOCKER (cross-owner write via provider-scoped `CALENDAR`) + 1 SHOULD-FIX (read→write escalation), both fixed by switching to granular non-provider-scoped `CREATE_CALENDAR`/`UPDATE_CALENDAR` (mirrors bundle write resources; only org-wide tokens reach the mutations, no owner-guard needed). Re-verified clean. Plan doc corrected (had said map to `CALENDAR`).
- **Summary**: Net-new `createCalendar`/`updateCalendar` public mutations for PERSONAL calendars carrying `is_private`. Gated by granular write resources. `update_calendar` is PERSONAL-only + org-scoped.

## Current phase
- Phase 7 — Gate codeless public group booking on `accepts_public_scheduling` (next; Tier 3 / sonnet; behavioral — breaking-change coordination).

## Remaining phases
- Phase 7 — Gate codeless public group booking on `accepts_public_scheduling` (behavioral; breaking-change coordination).

## Deferred phases
- None.
