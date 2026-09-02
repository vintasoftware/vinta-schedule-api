# Tracking — REST Code-Gated Scheduling

- **Plan**: `ai-plans/2026-09-01-REST_CODE_GATED_SCHEDULING_IMPLEMENTATION_PLAN.md`
- **Plan id**: `REST_CODE_GATED_SCHEDULING` (kebab: `rest-code-gated-scheduling`)
- **Started**: 2026-09-01
- **Last updated**: 2026-09-01
- **Feature flag**: none — the plan justifies "no flag, purely additive surface" in its **Guiding Decisions**. No flag-removal phase exists.

## Run options

| Option | Value |
|---|---|
| `pause_between_phases` | `false` |
| `generate_inline_comments` | `true` |
| `full_test_suite` | `false` (scoped) |
| `commit_strategy_resolved` | `stacked-branches` |
| `use_worktree` | `true` |
| `worktree_path` | `/Users/hugobessa/Workspaces/vinta/vinta-schedule-api/.claude/worktrees/plan-rest-code-gated-scheduling` |
| `worktree_branch` | `plan-rest-code-gated-scheduling` (base `f8a36e14`) |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-rest-code-gated-scheduling.yaml` |
| `sandbox_tier` | `enforced` on paper, but INERT for this session's in-process subagents (Claude Code reads the guard at session start, and this session is rooted in the main checkout). The active protection is the review-phase stray-write backstop, run after every implementer and fixer. |
| command surface | **container only** -- see below |

**Agent models** (from `.vinta-ai-workflows.yaml` `agent_models`): reviewer T3, fixer T2, worktree_prep T1, integrate T1. Per-phase reviewer overrides from the plan: Phase 0 → T4, Phase 3 → T3, Phase 5 → T3, Phase 6 → T4.

### Command surface — read before running anything

Every lint / test / type / migrate command runs through the container:

```
docker compose -p vinta-schedule-api run --rm --no-deps api uv run <command>
```

`-p vinta-schedule-api` reuses the already-running `db` / `broker` / `result` containers
instead of spawning a second set. `--no-deps` avoids a `mailpit` host-port conflict with an
unrelated `vinta-people-manager` project on this machine.

**Do not use bare `uv run` on the host here.** It reads `.env` → `localhost:5432`, where a
Homebrew PostgreSQL 14 shadows the docker PostgreSQL 18 container, and this plan's migration
`0051` uses PG15+ `ON DELETE SET NULL (column_list)` syntax that is a hard parse error on 14.
The container reaches PG18 as host `db` and is unaffected. CI runs `postgres:15`; Render runs 18.

Phase 0 and the first half of Phase 1 were verified on the host surface before this was
corrected; both were re-verified through the container afterwards. `WORKTREE.md` carries the
same guidance.

**Worktree resources** — dev DB `vinta_schedule_api_wt_plan-rest-code-gated-scheduling` and
test DBs `test_..._wt_plan-rest-code-gated-scheduling[_gwN]`, all inside the shared
`vinta-schedule-api-db-1` container (Postgres 18). `.venv` + `node_modules` symlinked to the main
checkout. Both `.env` (host surface) and `.env.docker` (container surface) now point at the forked
dev DB — note `prepare-worktree` forked only `.env` and copied `.env.docker` verbatim, so the
container surface pointed at the SHARED `vinta_schedule_api` database until this was corrected
during Phase 1. No compose override; the plan adds no services.

## Completed phases

### Phase 0 — Booking-code REST scaffolding, mint attribution, and duration pinning ✅

Branch `plan/rest-code-gated-scheduling/phase-0`, base `plan-rest-code-gated-scheduling` (`f8a36e14`).
Commits: `c14029df` (implementation), `7d186225` (reviewer findings).
Models used: implementer tier 3 → sonnet; reviewer tier 4 → opus; fixer tier 2 → sonnet.
16 files, +2567 / -7. Review: 0 BLOCKER, 9 SHOULD-FIX, all applied.

What landed:

- `public/booking/` mount (`booking_urls.py`, empty router) + `BookingCodeViewMixin`
  (`booking_views.py`) carrying only `authentication_classes = ()`,
  `permission_classes = ()`, and the DI `__init__`. Phases 1-5 import the helpers
  from `booking_auth` directly — the mixin deliberately holds no pass-through wrappers.
- `booking_exceptions.py`: one `BookingCodeAPIException` subclass per
  `BookingCodeErrorCode`, each carrying the plan's HTTP status; `OpaqueCodeError`
  (uniform read-side 403); `BookingCodeRangeError` (400). No change to
  `common/exception_handlers.py` was needed — DRF renders a dict `exc.detail` verbatim.
- `booking_auth.py`: header extraction, discriminated vs opaque code resolution,
  `client_ip_from_request`, `validate_code_gated_range`, `pinned_duration_error`.
- Migration `0051`: `duration` (nullable `DurationField`) + `minted_by_membership`
  (`OrganizationMembershipForeignKey`), `calmgmttoken_org_minter_idx` via
  `AddIndexConcurrently`, and the raw-SQL composite FK
  `ON DELETE SET NULL (minted_by_membership_user_id) DEFERRABLE INITIALLY DEFERRED`,
  added `NOT VALID` then validated. `atomic = False`.
- Duration pinning enforced in `CalendarPermissionService` via `_duration_pin_satisfied`,
  called before every `accepts_public_scheduling` short-circuit.

**Decisions later phases must respect:**

1. **`create_grouped_event` does NOT reach `can_perform_scheduling`.**
   `CalendarEventService.create_event` skips that gate when
   `event_data.group_authorized=True` (`calendar_event_service.py:609-623`), and
   `CalendarGroupService` always sets the flag. The pin therefore lives in a
   times-aware `can_perform_group_scheduling(group, *, start_time, end_time)`,
   whose only caller is `calendar_group_service.py:2642`. **Phase 2 must not assume
   the single-calendar gate covers group booking.** This corrects the plan's
   Phase 2 Changes item 3, which anticipated the question but not the answer.
2. **`MAX_CODE_GATED_RANGE` now lives only in `calendar_integration/booking_auth.py`.**
   `public_api/queries.py` imports it. Do not reintroduce a second literal.
3. **`resolve_code` now rejects a non-digit or >19-digit token id** as
   `InvalidTokenError` instead of letting Django's field coercion raise an uncaught
   `ValueError` (a 500 distinguishable from the uniform failure). Phase 5's six
   unauthenticated readers depend on this.
4. **`GET /public/booking/` returns 401, not 200**, for an anonymous caller —
   `DefaultRouter`'s `APIRootView` inherits `DEFAULT_PERMISSION_CLASSES`. Asserted in
   `test_booking_urls.py`.

Verification: full suite 5712 passed; mypy clean (771 files); `makemigrations --check`
clean; migration applies/reverses/reapplies on PG18; constraint confirmed
`convalidated` / `condeferrable` / `condeferred` with `SET NULL` scoped to the single column.

### Phase 1 -- Code-gated single-calendar booking

Branch `plan/rest-code-gated-scheduling/phase-1`, base `plan/rest-code-gated-scheduling/phase-0`.
Commits: `3fa6883d` (implementation), `15ac5518` (reviewer findings).
Models used: implementer tier 3 -> sonnet; reviewer tier 3 -> sonnet; fixer tier 2 -> sonnet.
8 files, +1075 / -5 before fixes. Review: 0 BLOCKER, 1 SHOULD-FIX, 1 NIT, both applied.

What landed: `POST /public/booking/calendar-events/` -- the first reachable endpoint on the
booking surface. `BookingCodeCalendarEventViewSet` (a bare `viewsets.GenericViewSet`, since the
project's `*VintaScheduleModelViewSet` bases mix in `TenantScopedViewMixin`, which needs request-bound
organization state this unauthenticated endpoint does not have) plus `BookingCodeEventCreateSerializer`.
`schema.yml` regenerated, additive only.

**Decisions later phases must respect:**

5. **`resolve_booking_code_from_request` now returns `tuple[token, code]`**, not just the token.
   Phases 2 and 4 must unpack it.
6. **create-then-consume ordering is NOT a DB-integrity guarantee.** Both statements sit inside one
   outer `transaction.atomic()`, so any exception unwinds everything and the DB outcome is
   ordering-independent -- the plan's stated rationale for the ordering was wrong. What create-first
   actually changes is that both racers reach the write adapter. The concurrency test now asserts
   `create_event.call_count == 2`, which is what makes an inversion fail. Phases 2 and 4 should copy
   the ordering for GraphQL parity, not for the reason the plan gave.
7. **Known pre-existing risk, out of scope:** on a provider-backed calendar, create-first means a
   losing racer may already have created an event at the external provider before the DB rolls back.
   That orphan is not undone by the rollback. Identical in the GraphQL original; recorded, not fixed.
8. **New surfaces reaching `create_event` must be registered in**
   `calendar_integration/management/commands/check_event_guarded_surfaces.py` -- a pre-commit gate
   blocks the commit otherwise. Phases 2 and 3 add such surfaces.

Verification (container surface): full suite 5730 passed, 1 skipped; mypy clean (772 files);
`makemigrations --check` clean (no migration this phase); 17 endpoint tests + 32 auth tests pass.

## Current phase

**Phase 2 — Code-gated group booking** — not started.

## Remaining phases

| Phase | Title | Impl tier | Reviewer tier |
|---|---|---|---|
| 2 | Code-gated group booking | 2 | default (3) |
| 3 | Codeless public group booking | 2 | 3 (plan override) |
| 4 | Code-gated reschedule and cancel | 3 | default (3) |
| 5 | Code-gated reads | 2 | 3 (plan override) |
| 6 | Booking-code minting and revocation | 3 | 4 (plan override) |

## Deferred phases

_none_ — every phase in this plan is in-repo, and the plan declares no feature flag, so there is no flag-removal phase to defer.
