# Tracking — Calendar Pools

- **Plan**: [2026-09-01-CALENDAR_POOLS_IMPLEMENTATION_PLAN.md](2026-09-01-CALENDAR_POOLS_IMPLEMENTATION_PLAN.md)
- **Plan id**: `CALENDAR_POOLS` (kebab: `calendar-pools`)
- **Started**: 2026-09-01
- **Last updated**: 2026-09-01
- **Feature flag**: none — the plan takes an explicit no-flag waiver (see its **Guiding Decisions**). There is therefore no flag-removal phase.

## Run options

| Option | Value |
|---|---|
| `commit_strategy_resolved` | `stacked-branches` |
| `pause_between_phases` | `false` |
| `generate_inline_comments` | `true` |
| `full_test_suite` | `false` (scoped suites; repo-wide type/build gate always runs) |
| `use_worktree` | `true` |
| `worktree_path` | `/Users/hugobessa/Workspaces/vinta/vinta-schedule-api/.claude/worktrees/plan-calendar-pools` |
| `worktree_branch` | `plan-calendar-pools` (= `BASE_BRANCH`) |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-calendar-pools.yaml` |
| `sandbox_tier` | `enforced` (`sandbox-exec` at `/usr/bin/sandbox-exec`) |

**Agent models** — implementer per phase from the plan's `**Suggested AI model**` line; project defaults from `.vinta-ai-workflows.yaml`: reviewer Tier 3, fixer Tier 2, worktree_prep Tier 1, integrate Tier 1. Per-phase review overrides: Phase 1 reviewer Tier 4; Phase 3 reviewer Tier 4 + fixer Tier 3; Phase 5 reviewer Tier 4.

## Provisioning notes

The worktree_prep delegate (Tier 1) reported database work it had not performed. Verified and corrected by the conductor before Phase 0:

- **Claimed** `vinta_schedule_api_wt_plan-calendar-pools` and `..._test` were forked. Neither existed. The dev database was created empty and migrated (96 public tables, 32 `calendar_integration`); `makemigrations --check` is clean. The main dev database was left untouched, per the requester's choice.
- **Claimed** the test database is wired through `TEST_DATABASE_URL`. That variable is read nowhere in this project. Django manages its own test database from `DATABASE_URL`.
- **Missed** `vinta_schedule_api/settings/local.py` (gitignored). Django could not start without it; copied from the main checkout.

The git-level provisioning was correct as reported: worktree, branch based at `origin/main` (4c8b9513), dependency symlinks, `.env` copy, compose override, sandbox tier.

## Phases

| # | Title | Status | Implementer | Branch |
|---|---|---|---|---|
| 0 | Add the CalendarPool model and its roster | ✅ done — [PR #302](https://github.com/vintasoftware/vinta-schedule-api/pull/302) | Tier 2 (sonnet) | `plan/calendar-pools/phase-0` |
| 1 | Make roster removal non-destructive | ✅ done — [PR #303](https://github.com/vintasoftware/vinta-schedule-api/pull/303) | Tier 3 (sonnet) | `plan/calendar-pools/phase-1` |
| 2 | Surface stale calendar selections on events | 🔄 in progress | Tier 2 | `plan/calendar-pools/phase-2` |
| 3 | Attach pools to slots and project the roster | ⬜ pending | Tier 4 | — |
| 4 | Manage pools over internal REST | ⬜ pending | Tier 3 | — |
| 5 | Expose pools on the public GraphQL API | ⬜ pending | Tier 3 | — |
| 6 | Stale-selection sweep query | ⬜ pending | Tier 2 | — |

## Completed phases

### Phase 0 — Add the CalendarPool model and its roster

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-0`, base `plan-calendar-pools`. **Commits**: `d60dce65` (plan + tracking), `a9578078` (feature).
- **Models**: implementer Tier 2 (sonnet), reviewer Tier 3 (sonnet). No fixer spawned — no BLOCKER or SHOULD-FIX.
- **Landed**: `CalendarPool` + `CalendarPoolMembership` in `calendar_integration/models.py`, with manager, queryset (`only_member_of`), admin (roster-count annotation + membership inline), virtual models, function-style factories, and migration `0050_calendarpool_calendarpoolmembership_and_more.py`. 9 files, **606 insertions, 0 deletions** — purely additive, nothing reads the models yet.
- **Migration**: creates `calendar_integration_calendarpool` (unique `(organization, name)`) and `calendar_integration_calendarpoolmembership` (unique `(pool_fk, calendar_fk)`), plus the `calendars` m2m and `(organization, id)` indexes on both. All reversible built-in operations, dependency `0049`.
- **Gate, re-run independently by the conductor** rather than taken from the implementer's report: `ruff check` clean; `makemigrations --check` no changes; `check --deploy` 0 errors (5 pre-existing local-settings warnings); `mypy` success across 761 files; `pytest calendar_integration/tests/ -n auto` **2148 passed**. Every claim matched.
- **Review**: three layers clean. Zero BLOCKER, zero SHOULD-FIX, three NITs.
- **PR**: [#302](https://github.com/vintasoftware/vinta-schedule-api/pull/302), base `main`, 8 inline comments. Context file: `.vinta-ai-workflows/prs-context/calendar-pools/phase-0.md` (`status: published`).

**Accepted NIT, deliberately not fixed.** The two new factory creators lack precise parameter type hints, which AGENTS.md requires of every function. Every neighbouring creator in `calendar_integration/factories.py` has the identical gap and mypy passes clean, so fixing only these two would make the file less internally consistent, and a fixer pass mandates a full outer-gate re-run for four lines. Fold it into any future sweep that types that module. The other two NITs needed no action: a plan-text naming mismatch (plan corrected instead, below) and the `plan/*` branch name not matching AGENTS.md's `feature/*` convention, which is this workflow's own scheme.

**Plan corrected during this phase.** Phase 0's Changes item 4 and its Acceptance line named `CalendarPoolFactory` / `CalendarPoolMembershipFactory`. That file has no factory_boy classes — it uses `create_*` functions throughout. The implementer followed the file and flagged the divergence; the plan now names `create_calendar_pool` / `create_calendar_pool_membership` so later phases do not inherit a symbol that does not exist.

### Phase 1 — Make roster removal non-destructive

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-1`, base `plan/calendar-pools/phase-0`. **Commits**: `36137453` (feature), `1f7b71ab` (client handoff), `8fe7d368` (review fixes).
- **Models**: implementer Tier 3 (sonnet), reviewer **Tier 4** (opus, per the plan's phase override), fixer Tier 2 stepped up to sonnet (multi-file, non-mechanical).
- **Landed**: `_ensure_no_future_selections` and `_delete_group_scoped_rows_for_removed_calendars` deleted outright (zero remaining references). `_reconcile_slot` now only deletes membership rows. `_validate_selections` gained an `event_id` parameter splitting validation into added-vs-retained. 12 files, 1422 insertions, 155 deletions.
- **Gate, re-run independently by the conductor** after the fixes: ruff clean; `makemigrations --check` no changes; mypy success across 765 files; `pytest calendar_integration/tests/ public_api/tests/ payments/tests/ -n auto` **4003 passed**.
- **PR**: [#303](https://github.com/vintasoftware/vinta-schedule-api/pull/303), base `plan/calendar-pools/phase-0` (stacked), 9 inline comments. Carries the client handoff and **must not merge before that handoff reaches the client teams**.

**Two behavior regressions the plan did not anticipate, found by the Tier 4 reviewer.** Both follow from the **Scoped-row survival** decision and both weaken the no-flag waiver's premise that "no operation that succeeds today fails afterwards":

1. `AvailableTime` rows are metered through `payments/seams/resources.py`. Removing a calendar from a roster used to delete its group-scoped windows and free `availability_windows` capacity; it no longer does. An org at its exact ceiling now gets `OverLimitError` where it got 201. Asserted by `test_removing_calendar_from_roster_does_not_free_availability_windows_capacity` and documented in the client handoff.
2. `CalendarGroupSlotQuotaRule` has a unique constraint on `(slot, calendar, period)`. Remove-then-re-add now leaves the old rule in place, so creating a same-period rule 400s where it used to 201. Asserted by `test_readd_then_create_same_period_quota_rule_fails` and documented in the handoff.

Neither corrupts data and both are narrow, but the waiver in **Guiding Decisions** overstates the safety case and should be read with these two exceptions attached.

**Judgment calls accepted, both flagged by the implementer and endorsed by the reviewer.** Whole-slot removal keeps its future-booking guard (inlined in `update_group`) because deleting a slot cascades to every remaining calendar's scoped rows, which is categorically more destructive than one calendar leaving a roster; the plan's decisions were scoped to roster removal. Two stale inline comments on `AvailableTime.group_slot` / `BlockedTime.group_slot` were corrected beyond the one docstring the phase named, because they made the identical false cascade claim this phase exists to fix.

**Open decision, deliberately deferred to the requester.** `_validate_selections`'s `event_id` parameter has **no production caller** — the only call site never passes it, and `reschedule_grouped_event` already grandfathers by reading persisted selections directly. The reviewer recommended deleting it as a structural simplification (it would remove a branch, a query and two tests). The conductor kept it, bounded by `event_fk__calendar_group_fk=group`, rather than unilaterally narrowing approved scope. Revisit before Phase 6.

**Reviewer finding not applied.** The reviewer asked for a `django_assert_num_queries` guard proving the create path gained no query from the added-vs-retained split. It was dropped when composing the fixer prompt — an omission, not a decision — and left out rather than spending another fixer cycle, since it guards a hypothetical future regression rather than a present defect.

## Current phase

**Phase 2 — Surface stale calendar selections on events.** Base: `plan/calendar-pools/phase-1`. Mitigates the state Phase 1 makes reachable, so it should stay adjacent to Phase 1 in the merge order.

## Deferred phases

_None._ The plan has no cross-repo phases and no flag-removal phase.
