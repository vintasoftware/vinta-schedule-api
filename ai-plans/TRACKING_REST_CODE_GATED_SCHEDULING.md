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

### Phase 2 — Code-gated group booking

Branch `plan/rest-code-gated-scheduling/phase-2`, base `plan/rest-code-gated-scheduling/phase-1`.
Commits: `033bff06` (implementation), `12e4a5fc` (reviewer findings — refactor).
Models used: implementer tier 2 → sonnet; reviewer tier 3 → sonnet; fixer tier 2 → sonnet.
7 files, +1208 / -3, then a 3-file dedupe refactor. Review: 0 BLOCKER, 2 SHOULD-FIX, 1 NIT, all applied.

What landed: `POST /public/booking/calendar-groups/<group_id>/events/`, code-gated only —
the codeless branch is Phase 3 and is deliberately absent. Plus a dedupe pass across
Phases 1 and 2 that the reviewer argued for on the grounds that Phase 3 forks this same
method again and Phase 4 adds two more near-copies.

**Decisions later phases must respect:**

9. **Two shared helpers now live in `calendar_integration/booking_auth.py`** and both write
   endpoints use them. Phases 3 and 4 must use them too rather than re-inlining:
   - `resolve_and_authorize_write(request, permission_service, required_permission)`
     → `tuple[token, code, Organization]`. Does code resolution, the permission assert, and
     org resolution. Deliberately does NOT do the scope check or the duration-pin check —
     those differ per endpoint and stay in the viewset.
   - `translate_booking_write_errors(*, permission_denied_message)` — a context manager
     mapping the six shared domain exceptions onto the API exception classes. `OverLimitError`
     is deliberately absent from it so it still reaches `vinta_exception_handler`'s 402.
10. **`_EndTimeAfterStartTimeSerializerMixin` in `serializers.py`** now carries the single
    `validate_end_time`. `BookingCodeEventCreateSerializer`,
    `BookingCodeGroupEventCreateSerializer`, and the pre-existing
    `CalendarGroupEventCreateSerializer` all inherit it.
11. **Path `<group_id>` vs token group mismatch returns 403, never 404.** The check is a pure
    in-memory int comparison against `token.calendar_group_fk_id` that runs before any
    `CalendarGroup` lookup, so a nonexistent id and a real-but-other-org id are
    indistinguishable. Two tests hold this. Phase 3's codeless branch must not weaken it.
12. **Small debt for Phase 3 to clear:** two docstrings in `booking_views.py` still say
    "see step 8 below" / "see step 9 below", but the inline `# --- Step N` comments were
    renumbered by the refactor. The fixer left them stale deliberately because those docstrings
    feed the OpenAPI `description` and it was told to keep `schema.yml` byte-identical.
    Phase 3 rewrites this method anyway — reword them to drop the step references entirely and
    let `schema.yml` pick up the description change.

Also note: the refactor moved the scope check to run after org resolution rather than between
the permission check and org resolution. Behaviorally inert today (every token has an
FK-backed organization, so org resolution cannot fail), and the full suite confirms it.

Verification (container surface): full suite 5753 passed, 1 skipped; mypy clean (773 files);
`makemigrations --check` clean (no migration this phase); `schema.yml` regenerated additive-only
by the feature commit and unchanged by the refactor; 21 group-endpoint tests plus 17 Phase 1
tests pass unmodified.

### Phase 3 — Codeless public group booking

Branch `plan/rest-code-gated-scheduling/phase-3`, base `plan/rest-code-gated-scheduling/phase-2`.
Commits: `f697efbc` (implementation), `efe25aee` (reviewer findings).
Models used: implementer tier 2 → sonnet; reviewer tier 3 (plan override) → sonnet; fixer tier 2 → sonnet.
5 files, +680 / -81. Review: 0 BLOCKER, 2 SHOULD-FIX, 2 NIT.

What landed: the codeless branch on the existing group endpoint, as a small `if code is None:`
fork rather than a second method. Authorization stays in the service —
`can_perform_group_scheduling` clause 1 short-circuits on `accepts_public_scheduling`.

**Decisions later phases must respect:**

13. **404-vs-403 asymmetry is deliberate.** On the CODED branch a path/token group mismatch
    returns 403, never 404, because the token's group is secret (decision 11). On the CODELESS
    branch a missing group returns 404, because the group id is the client's own input. Both
    sites carry comments saying not to "fix" one into the other.
14. **`booking_code_header` returns `None` for an empty header value**, so an empty
    `X-Booking-Code` takes the CODELESS branch, while a whitespace-only header is truthy and
    takes the CODED branch (rejected `404 INVALID_CODE`). Both are now locked in by
    `TestAmbiguousHeaderValues`. A whitespace header must never fall through to codeless — that
    would be a bypass.
15. **A codeless booking still creates a per-attendee RSVP `CalendarManagementToken`** as a
    normal `create_event` side effect, on both branches. "No code consumed" means no *booking
    code* is consumed; it does not mean no token row is written.

**BLOCKING FOLLOW-UP — Phase 3b.** The reviewer found that because the plan put no
`organization_id` in any path, the codeless branch is a global cross-tenant enumeration oracle:
an anonymous caller can walk `group_id` 1..N and learn from the 404/403/201 split whether a group
exists in ANY organization and whether it accepts public scheduling. GraphQL's
`createCalendarGroupEvent` requires BOTH `organization_id` and `group_id`, so this REST surface is
a strictly larger probing surface than its parity target, and throttling was declined. The user
chose the strongest remedy: an opaque, non-sequential public slug on `CalendarGroup`, addressed
instead of the integer PK. That is Phase 3b, stacked on this phase.
**Phase 3 must not be deployed without Phase 3b** — merging the stack is fine, deploying the
codeless surface keyed by integer id is not.

Verification (container surface): full suite 5765 passed, 1 skipped; mypy clean (774 files);
`makemigrations --check` clean; `schema.yml` regenerated by the feature commit
(description text plus `X-Booking-Code` becoming optional on this endpoint).

### Phase 3b — Opaque public slug for codeless group booking

Branch `plan/rest-code-gated-scheduling/phase-3b`, base `plan/rest-code-gated-scheduling/phase-3`.
Commits: `fbe054dc` (plan amendment), `e041fd4a` (implementation), `a55bc3c7` (reviewer findings).
Models used: implementer = `migration-author` tier 3 → sonnet; reviewer tier 4 → opus; fixer =
`migration-author` tier 3 → sonnet. 15 files, +1173 / -127. Review: 0 BLOCKER, 4 SHOULD-FIX, 5 NIT,
all applied. **Added mid-run**; the plan was amended in `fbe054dc` before implementation.

What landed: `CalendarGroup.public_booking_slug` (globally unique, `secrets.token_urlsafe(16)`),
a three-migration chain (0052 nullable add / 0053 backfill / 0054 unique + not-null), the group
route re-keyed from `<int:group_id>` to the slug on BOTH branches, and the slug exposed read-only
on `CalendarGroupSerializer` and in the admin.

**Decisions later phases must respect:**

16. **The coded branch never looks a group up BY the path value.** It dereferences
    `token.calendar_group` (keyed only on the token's own FK) and does an in-memory string compare
    against `public_booking_slug`. That is what makes the DB work identical for a nonexistent slug,
    another org's real slug, and the matching slug — no timing or status oracle. Phase 4 must not
    "simplify" this into a lookup by the path value.
17. **Two different slug generators exist on purpose.** New rows from Python use
    `secrets.token_urlsafe(16)` (22 chars). The DB column also carries
    `DEFAULT replace(gen_random_uuid()::text, '-', '')` (32 hex chars) purely as a deploy-window
    safety net: Render runs `migrate` in `buildCommand`, so old-code pods insert without the column
    while the migration is already applied, and without a DB default that is a `NotNullViolation`
    that a service rollback would NOT fix. Both are unguessable. Do not unify them.
18. **`0053`'s reverse is a no-op, deliberately.** NULLing slugs on reverse would permanently
    invalidate every booking link already distributed to patients, and a full reverse continues to
    `0052`, whose `RemoveField` drops the column anyway.
19. **`0054` carries a `DROP INDEX CONCURRENTLY IF EXISTS` guard** as its first operation, so a
    partial failure under `atomic = False` is resumable rather than wedged.

Verification (container surface): full suite 5777 passed, 1 skipped; mypy clean (779 files);
`makemigrations --check` clean; `sqlmigrate` read on all three migrations; chain applies, reverses
to 0051, and re-applies twice; a raw INSERT omitting the column produces distinct valid slugs
(`22818c94...`, `39045781...`), proving the deploy-window hazard is closed; live schema confirms
NOT NULL plus the DB default.

### Phase 4 — Code-gated reschedule and cancel

Branch `plan/rest-code-gated-scheduling/phase-4`, base `plan/rest-code-gated-scheduling/phase-3b`.
Commits: `300bceec` (implementation), `f646c343` (reviewer findings).
Models used: implementer tier 3 → sonnet; reviewer tier 3 → sonnet; fixer tier 2 → sonnet.
6 files, +2018 / -1. Review: 0 BLOCKER, 4 SHOULD-FIX, 1 NIT, all applied.

What landed: `POST /public/booking/events/reschedule/`, `POST /public/booking/group-events/reschedule/`,
and `POST /public/booking/events/cancel/` (204). All code-required; no codeless branch here.

**Decisions later phases must respect:**

20. **Cancel consumes BEFORE deleting — the opposite of the create endpoints.**
    `CalendarManagementToken.event` is `on_delete=CASCADE`, so deleting the event first would cascade
    the token away and the subsequent `consume_code` would fail. Matches the GraphQL original. The
    comments explain the difference; do not "harmonise" the two orderings.
21. **`resolve_and_authorize_write` now takes a keyword-only `permission_denied_message`.**
    Creates keep "This code does not permit booking."; reschedules pass "...rescheduling."; cancel
    passes "...cancellation." — matching each GraphQL original. Phase 5 should pass an appropriate
    message if it uses the helper.
22. **`booking_views.py` is now 1003 lines with five viewsets.** The reviewer's structural note:
    still coherent, but Phase 5 adds six more (read) viewsets — split into a write module and a read
    module as part of Phase 5 rather than growing this file to ~1600 lines.

**TRACKED FOLLOW-UP — pre-existing bug, NOT introduced by this plan, NOT fixed here.**
`serialize_event_data_input_util` (`calendar_integration/services/calendar_service_utils.py:435-444`)
builds its `resources=[...]` list by iterating a `Calendar` queryset under a loop variable named
`resource_allocation`, then accesses `.calendar` and `.status` on each item — attributes a `Calendar`
row does not have. Any reschedule of an event whose `ResourceAllocation` points at a calendar with
`calendar_type=CalendarType.RESOURCE` raises `AttributeError`. The reviewer verified this
independently. It predates this plan and is reachable through the GraphQL reschedule mutations
today, but Phase 4 makes it reachable from an **unauthenticated** endpoint, where it surfaces as a
500. Two separate test suites currently dodge it by not setting `calendar_type=RESOURCE`
(`test_booking_rest_reschedule.py`'s `resource_calendar` fixture and
`test_calendar_service.py::test_update_event_with_resource_allocations`), each with a docstring
saying so. **This deserves its own focused change and review, not a tail-end fix inside an unrelated
phase.** Suggested fix: iterate `ResourceAllocation` objects, or drop the misused join, so
`.calendar` / `.status` resolve.

Verification (container surface): full suite 5814 passed, 1 skipped; mypy clean (781 files);
`makemigrations --check` clean (no migration this phase); `schema.yml` additive-only from the
feature commit and unchanged by the fixes.

### Phase 5 — Code-gated reads

Branch `plan/rest-code-gated-scheduling/phase-5`, base `plan/rest-code-gated-scheduling/phase-4`.
Commits: `261118f5` (implementation), `270e3ace` (reviewer findings — includes a BLOCKER fix).
Models used: implementer tier 3 → sonnet; reviewer tier 3 (plan override) → sonnet; fixer tier 2 → sonnet.
Review: **1 BLOCKER**, 2 SHOULD-FIX, 1 NIT, all fixed.

What landed: the six code-gated reads in a new `calendar_integration/booking_read_views.py`
(the module split decision 22 asked for — `booking_views.py` already held only the mixin plus the
five write viewsets, so it took a zero-line diff). Every response serializer was reused; none written.

**THE BLOCKER — fixed on BOTH surfaces, and it was live in production GraphQL.**
`_resolve_calendar_scope_opaquely` read `token.calendar` and, when null, fell back to
`token.event.calendar`. A GROUP reschedule/cancel code carries `calendar_group_id` + `event_id` and
no `calendar_id`, while `create_grouped_event` always puts the underlying event on a real single
primary calendar — so that fallback resolved to the specific staff calendar the group booking landed
on. A patient holding a group reschedule code could call `available-times`, `availability-windows`,
`unavailable-windows`, or `calendar-bookable-slots` and get **200 with that individual's full
availability and blocked-time data** instead of the uniform 403. Verified before the fix by
reverting and re-running: the response body contained real `start_time`/`end_time` rows and
`"calendar":2`.

That defeats the group-booking abstraction (a patient is never supposed to learn which calendar
their appointment landed on) and contradicts the plan's own acceptance criterion. It was inherited
verbatim from `public_api/queries.py`, so the same hole existed in the six deployed GraphQL
`*WithCode` query fields. **Both surfaces are now guarded**: each resolver rejects immediately when
the token carries the opposite scope column, before ever consulting the `event` fallback. This is a
behavior fix, not a schema change, so the "no GraphQL schema change" non-goal still holds.

**Decisions later phases must respect:**

23. **Never resolve scope through `token.event` without first checking the token's own scope column.**
    The `event` fallback is only valid when the token carries no scope of its own.
24. **Read endpoints live in `calendar_integration/booking_read_views.py`**, writes stay in
    `booking_views.py`, and `BookingCodeViewMixin` is shared from the latter.
25. **`duration_seconds` is now required on both bookable-slots reads regardless of pin state.**
    Presence is validated identically whether or not the code pins a duration; only the *value* is
    overridden by a pin. This removes a status asymmetry that distinguished pinned from unpinned.
26. **Timezone-naive datetimes are rejected with 400** on the five GET reads that take datetime
    query params, matching GraphQL's scalar, rather than being silently interpreted in the default
    timezone.

The non-disclosure matrix now covers 8 failure kinds (invalid, expired, already-used, revoked,
wrong-scope, wrong-scope-via-event-fallback, missing header, empty header) × 6 endpoints, asserting
byte-identical bodies — 96 assertions.

Verification (container surface): full suite 5851 passed, 1 skipped, no flakes; mypy clean
(783 files); `public_api/tests/` 1043 passed (the GraphQL fix); `makemigrations --check` clean;
`schema.yml` changed only for the `duration_seconds` parameter description and `required: true`.

### Phase 6 — Booking-code minting and revocation

Branch `plan/rest-code-gated-scheduling/phase-6`, base `plan/rest-code-gated-scheduling/phase-5`.
Commits: `c42d250e` (implementation), `228d47a1` (BLOCKER fix), `68f0ba9b` (same hole on GraphQL).
Models used: implementer tier 3 -> sonnet; reviewer tier 4 (plan override) -> opus; fixer tier 2 -> sonnet.
Review: **1 BLOCKER**, 5 SHOULD-FIX, 4 NIT, all fixed.

What landed: `POST /booking-codes/` and `DELETE /booking-codes/<id>/`, authenticated with
session/JWT + active org membership, registered in `routes.py` (NOT the public namespace). One
endpoint collapses the six GraphQL mint mutations via `purpose` x target. No `list`, no `retrieve`.

**THE BLOCKER — privilege escalation with permanent, untraceable effect.**
`DELETE /booking-codes/<id>/` had no authorization beyond "has an active membership", and
`revoke_token` did not filter by token *kind*. Any org member could walk ids and revoke EVERY
`CalendarManagementToken` in the organization — including other users' calendar-owner and attendee
tokens. Demonstrated before the fix: a plain member revoked another user's calendar-owner token, and
that user's `initialize_with_user` then raised `PermissionServiceInitializationError` — locked out of
their own calendar. It never healed, because `create_calendar_owner_token`'s `get_or_create` has no
`revoked_at` in its lookup and returns the revoked row forever. And it audited as `system_actor()`,
so nothing recorded who did it.

Fixed in two layers, and on BOTH surfaces:

- `228d47a1` — the REST `destroy` now applies the same owner-or-admin check `create` applies, and
  returns `204` for BOTH "not found" and "not authorized", so the non-oracle contract survives.
- `68f0ba9b` — `revoke_token` ITSELF now resolves through
  `CalendarManagementToken.objects.booking_codes_for_organization(...)`, so it can only ever touch a
  booking code. That closes the identical hole on the **already-deployed** GraphQL
  `revokeBookingCode` mutation, which called the service directly. Revoke is now attributable on
  both surfaces rather than auditing as `system`.

**Decisions to carry forward:**

27. **The booking-code discriminator is `minted_by_membership_user_id IS NOT NULL OR
    minted_by_system_user_id IS NOT NULL`** (`CalendarManagementTokenQuerySet.booking_codes()`).
    Verified sound against every `create_*_token` method: owner, attendee, and external-attendee
    tokens always leave both null, and every booking-code mint path sets exactly one.
    **Known fragility:** a future mint path that passes neither actor would produce a booking code
    that is silently UN-REVOKABLE. Twelve test files had to be updated for exactly this reason —
    their fixtures minted unattributed tokens. If a legitimate actor-less mint path is ever needed,
    replace this heuristic with an explicit kind column rather than widening the predicate.
28. **`CalendarPermissionService.can_view_calendar(user, calendar)`** now exists next to
    `can_view_calendar_group`, and both halves of the owner-or-admin split go through the service
    rather than a hand-rolled query in the view.

Verification (container surface): full suite 5895 passed, 1 skipped; mypy clean (784 files);
`public_api/tests/` 1046 passed, covering the GraphQL revoke fix; `makemigrations --check` clean
(Phase 0 already added the columns); `schema.yml` additive only.

## Current phase

_none — all phases complete._

## Remaining phases

| Phase | Title | Impl tier | Reviewer tier |
|---|---|---|---|

## Deferred phases

_none_ — every phase in this plan is in-repo, and the plan declares no feature flag, so there is no flag-removal phase to defer.
