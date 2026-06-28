# Tracking — Bookable Slots for Single Calendars & Bundles, with Booking Policies

- **Plan**: `ai-plans/2026-06-26-BOOKABLE_SLOTS_SINGLE_CALENDAR_AND_BOOKING_POLICY_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-27
- **Last updated**: 2026-06-28
- **Feature flag**: none — data-presence gate (no policy ⇒ unchanged behavior).

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `commit_strategy_resolved`: stacked-branches
- `use_worktree`: true
  - `worktree_path`: `.claude/worktrees/plan-bookable-slots-booking-policy`
  - `worktree_branch`: `plan/bookable-slots-single-calendar-and-booking-policy/wt`
  - `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-bookable-slots-booking-policy.yaml`
  - `sandbox_tier`: enforced (sandbox-exec present; claude-code spawns subagents in-process, so the Layer 1 post-run stray-write check is the operative backstop)

## Branch stack
- Phase 1: `plan/bookable-slots-single-calendar-and-booking-policy/phase-1` (base `main`)
- Phase 2: `plan/bookable-slots-single-calendar-and-booking-policy/phase-2` (base phase-1)

## Completed phases

### Phase 1 — Scaffold the BookingPolicy model ✅
- **Status**: DONE (reviewed, fixed, pushed, PR open)
- **Model used**: opus (plan tier: T1 model / T4 migration → migration-author agent)
- **Branch**: `plan/bookable-slots-single-calendar-and-booking-policy/phase-1` (base `main`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/166
- **Commits**: `47aaecc` (impl) + `5b3c3de` (review fixes)
- **Summary**: Added `BookingPolicy(OrganizationModel)` — nullable target FKs (calendar/membership/calendar_group via composite OrganizationForeignKey/OrganizationMembershipForeignKey) + `is_organization_default`, four `PositiveIntegerField` second-counts (0 = no constraint). `bookingpolicy_exactly_one_target` check constraint (4-way disjunction) + 4 per-target partial unique indexes. Migration `0039` (CreateModel) + `0040` (raw-SQL composite PROTECT FK for membership, `ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`, NOT VALID+VALIDATE, atomic=False — mirrors siblings). `BookingPolicyManager`/`BookingPolicyQuerySet` org-scoped lookups (`for_target`/`org_default` take `organization_id`), admin, `create_booking_policy` factory, 23 unit tests.
- **Review findings fixed**: BLOCKER — missing membership composite-FK migration (added 0040); BLOCKER — manager `for_target`/`org_default` lacked `filter_by_organization` → always raised `ImproperlyConfigured` (added required `organization_id`); SHOULD-FIX — membership `on_delete` CASCADE→PROTECT to match sibling pattern; queryset docstrings reworded; added membership-orphan + manager-org-scope tests; NIT — redundant manager annotation.
- **Gates**: ruff clean; mypy baseline unchanged (298, no new); `makemigrations --check` clean; `check --deploy` 0 errors; full `pytest -n auto` → 3244 passed (order-flakes confirmed pre-existing). Migration round-trip verified.
- **Acceptance**: ✅ migration applies + reverses; single-target enforced; duplicate/multi-target inserts raise IntegrityError; membership integrity enforced at commit.

### Phase 2 — Effective-policy resolver service ✅
- **Status**: DONE (reviewed, fixed, pushed, PR open)
- **Model used**: sonnet (plan tier T3) — implementer + sonnet fixer
- **Branch**: `plan/bookable-slots-single-calendar-and-booking-policy/phase-2` (base phase-1)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/167
- **Commits**: `e0e79f6` (impl) + `ae265d4` (review fixes)
- **Summary**: `EffectivePolicy` frozen dataclass (`unconstrained`/`from_model` [horizon 0→None]/`most_restrictive` [max lead+buffers, min finite horizon]) in `services/dataclasses.py`. `BookingPolicyService` (`services/booking_policy_service.py`): `resolve_for_calendar` (calendar→owning-membership [lone/`is_default`/skip]→org-default→unconstrained), `resolve_for_bundle`/`resolve_for_group` (explicit→most-restrictive across all participants via `resolve_for_calendar`→unconstrained), create/update/delete (exactly-one-target, `DuplicateBookingPolicyError`, immutable targets, idempotent delete-absent, AuditService records w/ diffs). DI Factory `booking_policy_service` in `di_core/containers.py`. 63 service tests.
- **Review findings fixed**: no BLOCKERs. SHOULD-FIX — moved `DuplicateBookingPolicyError` to `exceptions.py` (subclass `CalendarIntegrationError`); annotated `get_all_policies -> BookingPolicyQuerySet`; added tests (no-change `diff is None`, bundle-unconstrained-with-owners, membership/group delete-absent no-ops). NIT — late-import rationale comment, `_actor: ActorSnapshot | None` typing.
- **Confirmed-intended behavior** (reviewer flagged, no change): bundle/group resolution inherits the org-default via participant `resolve_for_calendar`, so a bundle/group is never truly unconstrained while an org-default exists. Matches plan ("never offer a slot a participant would reject"); annotated in PR + asserted by `test_child_inherits_org_default_if_no_direct_policy`. Phase 5/7 consumers depend on this.
- **Gates**: ruff clean; mypy baseline unchanged; `makemigrations --check` clean (no migration); `check --deploy` 0 errors; full `pytest -n auto` → 3307 passed.
- **Acceptance**: ✅ `resolve_for_*` returns spec-correct `EffectivePolicy` across the full matrix; unconstrained when nothing applies.

## Current phase
Phase 3 — Policy CRUD private REST (next).

## Remaining phases
- Phase 3 — Policy CRUD private REST (T2 / haiku)
- Phase 4 — Policy CRUD public GraphQL (T3 / sonnet)
- Phase 5 — calendar_bookable_slots single+bundle (T4 / opus)
- Phase 6 — calendar_bookable_slots_with_code (T2 / haiku)
- Phase 7 — group query policy-aware (T3 / sonnet)
- Phase 8a — enforcement single/bundle/code (T3 / sonnet)
- Phase 8b — enforcement group (T3 / sonnet)

## Deferred phases
None (no cross-repo, no flag-removal — data-presence gate means no flag).
