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
| 2 | Surface stale calendar selections on events | ✅ done — [PR #305](https://github.com/vintasoftware/vinta-schedule-api/pull/305) | Tier 2 (sonnet) | `plan/calendar-pools/phase-2` |
| 3 | Attach pools to slots and project the roster | ✅ done — [PR #307](https://github.com/vintasoftware/vinta-schedule-api/pull/307) | Tier 4 (opus) | `plan/calendar-pools/phase-3` |
| 4 | Manage pools over internal REST | ✅ done — [PR #309](https://github.com/vintasoftware/vinta-schedule-api/pull/309) | Tier 3 (sonnet) | `plan/calendar-pools/phase-4` |
| 5 | Expose pools on the public GraphQL API | ✅ done — [PR #315](https://github.com/vintasoftware/vinta-schedule-api/pull/315) | Tier 3 (sonnet) | `plan/calendar-pools/phase-5` |
| 6 | Stale-selection sweep query | 🔄 in progress | Tier 2 | `plan/calendar-pools/phase-6` |

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

### Phase 2 — Surface stale calendar selections on events

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-2`, base `plan/calendar-pools/phase-1`. **Commits**: `853f6d02` (feature), `c1007c31` (review fixes + REST wiring).
- **Models**: implementer Tier 2 (sonnet), reviewer Tier 3 (sonnet), fixer Tier 2 stepped up to sonnet.
- **Landed**: `isInCurrentRoster` on the GraphQL selection type via a batch helper, `is_in_current_roster` on `CalendarEventGroupSelectionSerializer`, and a nested read-only `group_selections` on `CalendarEventSerializer`. Staleness predicate is exactly the plan's: no `CalendarGroupSlotMembership` row for the `(slot, calendar)` pair, which stays correct once Phase 3 projects pool calendars into that table.
- **Gate**: ruff clean; format clean; `makemigrations --check` no changes; mypy success across 766 files; `pytest calendar_integration/tests/ public_api/tests/ -n auto` **3176 passed** (= the full collected count), 0 errors.

**Scope decision by the requester, mid-phase.** The REST field was initially unreachable: `CalendarEventGroupSelectionSerializer` was nested in nothing and mounted on no route, so the phase could not meet its own "on both REST and GraphQL" acceptance line. Options were to drop the REST half, wire it in, or ship it as documented scaffolding. The requester chose to **wire it in**, so `CalendarEventSerializer` now exposes a nested `group_selections` array and `schema.yml` was regenerated. This is an additive REST response-shape change the plan never scoped; it is documented in the client handoff.

**Known cost of that decision.** The virtual-model prefetch runs unconditionally, so **every event REST fetch now costs one extra query** whether or not the client reads `group_selections`. That surfaced as a pinned-count update in `calendar_integration/tests/test_views.py` (ICS download, 26 → 28: one prefetch across that view's two `get_optimized_queryset` calls). The constant was updated with an explanation rather than the assertion loosened, matching the same file's precedent from the External Client Identifiers phase.

**Pre-existing defect found, deliberately not fixed.** `CalendarVirtualModel.calendar_ownerships` (`calendar_integration/virtual_models.py:36`) names a field that is not `Calendar`'s accessor — `Calendar.ownerships` is, per `calendar_integration/models.py:269`; `calendar_ownerships` is `OrganizationMembership`'s related name. django-virtual-models' "empty lookup list means prefetch everything" fallback raises `AttributeError` when it walks that branch. It had never been exercised before this phase. Both the implementer and the fixer avoided it by terminating the lookup on a concrete column rather than patching the field, and `virtual_models.py` has zero diff lines across this phase. **Worth its own ticket** — the workaround is sound but the bug remains latent for the next caller.

**Plan deviation, recorded per the reviewer's request.** The plan's Touch List lists `calendar_integration/virtual_models.py` as a Phase 2 edit. It was never touched: `CalendarGroupSlotVirtualModel.memberships` already existed from Phase 0 and was exactly what the prefetch hint needed, so no edit was warranted.

**Test-infrastructure observation, not caused by this phase.** One scoped-suite run produced 16 errors on `test_request_calendar_sync_task_not_fired_on_rollback` and siblings; the test passes in isolation in 35.5s against `pytest.ini`'s 60s per-test timeout, and a clean re-run passed all 3176. Separately, two runs reported *more* passing tests (3201, 3209) than the 3176 that actually exist, which could not be reconciled — an xdist/timeout/coverage interaction worth investigating independently.

### Phase 3 — Attach pools to slots and project the roster

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-3`, base `plan/calendar-pools/phase-2`. **Commits**: `82dfd871` (feature), `1890c7a0` (review fixes).
- **Models**: implementer Tier 4 (opus), reviewer Tier 4 (opus), fixer Tier 3 (sonnet).
- **Gate, re-run by the conductor in the worktree**: ruff clean; `makemigrations --check` no changes; mypy success across 772 files; **3254 tests collected, 3254 passed**. Migration verified forward → reverse → forward against a deliberately constructed inline-plus-projected row.

**Constraint form — the plan was wrong and is now corrected.** `NULLS NOT DISTINCT` requires Postgres 15+. Local development runs **14.23**, CI runs `postgres:15`, docker-compose runs `postgres:alpine` (unpinned, currently 18). The original instruction would have passed CI and failed locally. What shipped is a pair of partial unique indexes, verified by the reviewer as provably equivalent by case analysis over all four NULL combinations. **The three-way Postgres version skew is a standing project risk unrelated to this feature** — pinning `postgres:alpine` is the cheap first step.

**BLOCKER found and fixed: pool roster edits did not reproject.** Phase 0 registered `CalendarPoolAdmin` with an editable membership inline. Once Phase 3 projected from that roster, removing a calendar from a pool in the admin left the projected `CalendarGroupSlotMembership` row in place, so `_validate_selections` still saw it and would **accept a brand-new booking against a calendar the organization had just removed** — the same failure class Phase 1 was written to prevent, and a direct violation of the **Drift mitigation** decision. Closed with `post_save`/`post_delete` receivers in `calendar_integration/signals.py` that reproject into every attached slot inside the caller's transaction, plus a `CalendarPoolMembershipQuerySet.delete()` override so a bulk delete reconciles once per pool rather than once per row. `bulk_create` fires no signal and nothing calls it on that model today; that is documented in the signals module as a landmine for future callers.

**Two API lockouts found and fixed.** Validation ran against pool attachments the caller had not submitted, so a third party editing an unrelated pool could make a group permanently uneditable — failing with an error naming a slot the caller never touched. Validation now considers only explicitly-submitted `pool_ids`, which also removed an extra query on every `update_group` for groups with no pools. That acceptance is now pinned by a query-count test.

**Other plan errors found by the implementer.** (1) `_resolve_group_scoped_membership` used `.get()` on `(slot, calendar)`, which becomes non-unique under projection — a `MultipleObjectsReturned` 500 across six group-scoped write paths; now `.order_by("id").first()`. (2) `_validate_slots_input` required at least one *inline* calendar, which would have made a pool-only slot — the feature's motivating case — unbuildable. (3) Duplicate calendars leaked into four read surfaces including duplicate *groups* in a filtered list; all deduplicated. (4) `pool_ids` typing contradicted the omit-means-unchanged rule.

**Verified, not assumed.** The two versioned quota Postgres functions take row ids and never reference the roster table, so the plan's claim held and no version bump was needed. The whole `migrations/sql/` tree has zero references to `calendargroupslotmembership`.

**Deferred to Phase 4 by the implementer, with reasoning.** `CalendarGroupSlotVirtualModel` does not gain `pools` in this phase — `CalendarGroupSlotSerializer` has no `pools` field until Phase 4, and adding an unconditional prefetch now would cost one extra query on every group fetch and jeopardise this phase's no-extra-queries acceptance. Phase 4 should add the hint together with its serializer field.

**Carried into Phase 4.** A slot **renamed** in an `update_group` payload is deleted and recreated by name, and the recreated slot gets `pool_ids=None` → no pools, silently detaching. Documented with a comment; carrying attachments across a rename needs an input-shape change the current dataclass cannot express.

**Noted, not fixed.** `calendar_integration/signals.py` calls the private `CalendarGroupService._reconcile_slot_pools` with a `# noqa: SLF001`. Pragmatic and documented, but worth promoting to a public service method if Phase 4 touches that code.

### Phase 4 — Manage pools over internal REST

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-4`, base `plan/calendar-pools/phase-3`. **Commits**: `a2fa6e7d` (feature), `8f035d24` (PATCH data-loss fixes).
- **Models**: implementer Tier 3 (sonnet), reviewer Tier 3 (sonnet), fixer Tier 2 stepped up to sonnet.
- **Landed**: `CalendarPoolViewSet` + serializer + permission + filterset + routes, `create_pool`/`update_pool`/`delete_pool` on the service, `pool_ids`/`pools` on the slot serializer, the deferred `CalendarGroupSlotVirtualModel.pools` prefetch with a query-count guard, and `schema.yml` regenerated (+625 lines).
- **Gate, re-run by the conductor in the worktree**: **3284 collected, 3284 passed**; ruff clean; mypy success across 773 files; no schema drift.

**BLOCKER found and fixed: PATCH was destroying data.** `CalendarPoolViewSet` has no `http_method_names` override, so `PATCH /calendar-pools/{id}/` was live. Under DRF partial-update semantics an absent field never reaches `validated_data`, so `PATCH {"description": "x"}` raised `KeyError` → 500, and `PATCH {"name": "Renamed"}` read `calendar_ids` as `[]` → **wiped the entire pool roster and cascaded that wipe into every attached slot**. The new test file had zero PATCH coverage; every test used PUT.

**The same defect already existed on `main` and was fixed here at the requester's direction.** `CalendarGroupSerializer._to_input_data` uses `validated_data.get("slots", [])`, so `PATCH /calendar-groups/{id}/` omitting `slots` — a routine rename — deleted every slot in the group, or 409'd if a slot had a future booking. Confirmed present on `origin/main`, so it predates this plan entirely. It was findable only because the new `pool_ids` handling next to it was *correct*, which prompted the reviewer to ask why the adjacent `slots` handling was not.

Both fixed by **rejecting the ambiguous partial write** rather than making the service delete less aggressively — replace-semantics are correct for PUT and other code relies on them; the defect was an absent key being read as an empty value. PUT behavior is byte-identical and no pre-existing test was modified (0 deleted lines in the existing group test file). PATCH stays enabled because `schema.yml` documents it.

**Fifth plan error, found by the implementer.** The **Visibility scoping** decision specifies REST permissions using Public-API *token scope* vocabulary (`org_wide` / `scoped_admin` / `scoped_member`, `scoped_calendar_group_queryset`) that belongs to Phase 5's GraphQL surface. REST has no token scope; the axis is organization-admin vs member. The implementer mirrored `CalendarGroupPermission` rather than inventing a REST reading of GraphQL names. Consequence: "fail closed" on REST is a **403 from `has_permission`**, not the 200-with-empty-list the plan's wording implies. The implementer wrote its test to the plan's literal wording, got 403, and corrected the test rather than weakening the permission class.

**Verified by the reviewer, for the record.** The 409-on-in-use-delete does not leak group names to unauthorized callers — the admin gate in `has_permission` runs before `get_object()` and `delete_pool`, and the referencing-group query is itself organization-scoped. The two-pass `update_pool` reconcile is contained in one `transaction.atomic()` with no externally observable intermediate state. The group-list query guard asserts invariance against a measured baseline rather than a magic number.

**Noted, not fixed.** `calendar_integration/signals.py`'s docstring says "nothing in this codebase calls `CalendarPoolMembership.objects.bulk_create` today". That was already stale when Phase 4's `update_pool` shipped and is doubly stale now that `create_pool` uses it too. Both callers reconcile explicitly, so the behavior is correct — but the comment is a safety note that is now wrong and should be corrected.

**Same defect class audited elsewhere, none vulnerable.** `CalendarBundleUpdateSerializer` is wired to a PATCH action but instantiated without `partial=True`. The three group-scoped update serializers use `partial=True` deliberately with all-optional fields. `CalendarEventSerializer.update` already reconstructs absent collections from `instance` rather than defaulting to `[]` — the pattern the rest should follow if this is ever generalized.

### Phase 5 — Expose pools on the public GraphQL API

- **Status**: complete. **Branch**: `plan/calendar-pools/phase-5`, base `plan/calendar-pools/phase-4`. **Commits**: `8785289a` (feature), `71468775` (review fixes).
- **Models**: implementer Tier 3 (sonnet), reviewer **Tier 4** (opus, per the plan's override), fixer Tier 2 stepped up to sonnet.
- **Gate, re-run solo by the conductor**: **3327 collected, 3327 passed**, 0 errors; ruff clean; `makemigrations --check` no changes; mypy success across 775 files.

**A confirmed production vulnerability was found during this phase and fixed separately.** While verifying this phase's report, the conductor traced and then proved — with an end-to-end HTTP probe — that `createCalendarGroup` / `updateCalendarGroup` / `deleteCalendarGroup` accepted **unauthenticated cross-tenant writes**: no `permission_classes`, organization taken from client input. Fixed on `hotfix/public-api-group-mutation-auth` off `main` and **merged as [PR #313](https://github.com/vintasoftware/vinta-schedule-api/pull/313)**, independent of this stack. `createCalendarGroupEvent` was gated too — it was unexploitable only because a DI double-instantiation bug breaks it for everyone, which is a coincidence rather than a control.

This phase did **not** extend the hole: its implementer was told to mirror sibling mutations, looked at `CalendarGroupMutations`, and refused to copy the pattern — building the pool mutations with `permission_classes` and token-derived organization from the start. The Tier 4 reviewer's probe confirmed every new pool field rejects anonymous and wrong-resource requests, against a positive control that still reproduced the (then-unfixed-on-this-branch) group vulnerability.

**BLOCKER found and fixed: duplicate pool name leaked database schema.** `CalendarPool` has `UniqueConstraint(organization, name)` but neither `create_pool` nor `update_pool` validated it, and `IntegrityError` is not a `CalendarPoolError`, so a duplicate name escaped as a raw Postgres message in `errors[]` — disclosing the constraint name, its column tuple and a raw `organization_id` to a partner token, with `data: null` so clients following the documented data-not-errors contract had no `success` field to read. Fixed with pre-write validation plus a **savepoint-scoped `IntegrityError` backstop**, so the concurrent-create race cannot leak the raw message either.

**Seven SHOULD-FIX items, all applied.** The most consequential: (1) the new suite had **no unauthenticated tests** — the gate was correct but unpinned, which is exactly the gap that let the group vulnerability survive review; behavioral tests now assert refusal *and* unchanged database state for all five fields. (2) The nested `slots { pools }` field bypassed the resource gate, letting a token holding only `CALENDAR_GROUP` read every pool name and roster in the org; now returns `[]` without `CALENDAR_POOL`. (3) `create_pool` never called `_check_not_restricted()`, so a suspended tenant could create pools but not edit them. (4) `OverLimitError` was uncaught in two mutations, bypassing the structured error contract. (5) An N+1 on the singular `calendarGroup` path (35 queries at 4 slots × 4 pools). (6) Query guards were magic numbers rather than invariance assertions — now compare two fixture sizes. (7) `referencingGroups` disclosed group names to a token denied `CALENDAR_GROUP`.

**The invariance guards paid for themselves immediately.** The fixer's first implementation of the resource check queried per-slot, and the newly-rewritten two-size guards caught the N+1 it introduced. A magic-number guard would have been "fixed" by updating the constant.

**Noted, not fixed.** `CalendarGroupSlotGraphQLType.calendars` leaks `Calendar` data to a token without the matching resource by the same mechanism the nested `pools` field did — pre-existing, out of scope, worth a ticket. `scoped_calendar_pool_queryset` was a byte-identical copy of `scoped_calendar_group_queryset`; both now delegate to a shared generic helper, since a security predicate duplicated verbatim is how two copies drift apart.

**Test-infrastructure note, corrected.** Intermittent `-n auto` errors seen across several phases are `psycopg.errors.ObjectInUse` on xdist worker databases, caused by **two pytest runs executing concurrently against the same worker DB names** — not the 60s per-test timeout this tracking file previously blamed. A solo run of this phase was clean at 3327/3327.

## Current phase

**Phase 6 — Stale-selection sweep query.** Base: `plan/calendar-pools/phase-5`. The final phase: a read-only service method plus REST and GraphQL surfaces listing stale `(event, slot, calendar)` triples.

**Fixer pass: pagination collapsed into the service, and the Phase 1 dangling parameter resolved.** Four SHOULD-FIX findings plus a NIT, all closed:

1. **Pagination was broken three ways** — GraphQL sliced a fully-materialized Python list after fetching every stale row; REST had no bound at all; neither went through the project's `_slice_qs` convention. `find_stale_selections` (`calendar_integration/services/calendar_group_service.py`) now takes `offset`/`limit`, validates them the same way `public_api.queries._slice_qs` does (reject negative offset, cap limit at 100), and applies the bound at the queryset level — `LIMIT`/`OFFSET` in SQL, before `values_list()` evaluates it — so a caller can never force the whole backlog to materialize. Both the REST action and the GraphQL resolver now pass their `offset`/`limit` straight through instead of reimplementing bounds-checking themselves, and both translate the shared `CalendarGroupValidationError` into their own surface's error shape (DRF `ValidationError` / `GraphQLError`). Pinned by a test that inspects the captured SQL for the `LIMIT` clause against a 20-row stale set.
2. **`schema.yml` documented pagination the endpoint didn't implement** — `stale-selections` inherited `CalendarGroupViewSet`'s default `pagination_class`/`filterset_class` via drf-spectacular introspection, advertising a `count`/`next`/`previous`/`results` envelope and a working `name` filter, neither of which the bare-array action implemented. Resolved by keeping the bare-array response (matching the plan and the GraphQL surface's shape) and making its `offset`/`limit` genuinely work now that the service supports bounds, with `pagination_class=None, filter_backends=[]` on the action so the schema stops advertising the envelope and the phantom `name` param. `schema.yml` regenerated. The sibling `booked-events` action carries the identical drift and was deliberately left alone — out of scope for this finding.
3. **`_validate_selections`'s dangling `event_id` parameter (flagged in Phase 1, "revisit before Phase 6") — deleted.** Confirmed zero callers pass it anywhere in the codebase (the only call site, `create_group`, has never passed it) and confirmed `reschedule_grouped_event` grandfathers a departed calendar by reading persisted `CalendarEventGroupSelection` rows directly, never through this method — so the parameter's branch was dead code guarding nothing. Removed the parameter, the branch, and the two tests in `calendar_integration/tests/services/test_calendar_group_service_lenient_removal.py` that only existed to exercise it (`test_validate_selections_grandfathers_departed_calendar_without_addition`, `test_validate_selections_still_rejects_genuinely_new_calendar_on_update`). No behavior change on any live path — `_validate_selections`'s only real caller (`create_group`) never set `event_id`, so it always took the `None` branch already.
4. **NIT — date-window boundary coverage.** Added two boundary tests at the service layer: an event whose `end_time` equals `window_start` exactly, and one whose `start_time` equals `window_end` exactly — both excluded, pinning the half-open (`__gt`/`__lt`) exclusivity by test rather than by code reading alone.

**Gate, re-run solo:** ruff clean; ruff format clean; `makemigrations --check` no changes; `check --deploy` 5 pre-existing local-settings warnings, 0 errors; mypy success across 776 files; `pytest calendar_integration/tests/ public_api/tests/ -n auto` **3359 passed**.

## Deferred phases

_None._ The plan has no cross-repo phases and no flag-removal phase.
