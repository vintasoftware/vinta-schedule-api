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
- Phase 3: `plan/bookable-slots-single-calendar-and-booking-policy/phase-3` (base phase-2)
- Phase 4: `plan/bookable-slots-single-calendar-and-booking-policy/phase-4` (base phase-3)
- Phase 5: `plan/bookable-slots-single-calendar-and-booking-policy/phase-5` (base phase-4)

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

### Phase 3 — Policy CRUD private REST ✅
- **Status**: DONE (reviewed, fixed, pushed, PR open)
- **Model used**: sonnet (plan tier T2 → stepped up per >3-files rule) — implementer + sonnet fixer
- **Branch**: `plan/bookable-slots-single-calendar-and-booking-policy/phase-3` (base phase-2)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/168
- **Commits**: `1abddaf` (impl) + `6e5efeb` (review fixes)
- **Summary**: `BookingPolicySerializer` (exactly-one-target, immutable targets on update, org-scoped FK querysets, `validate_membership_user_id`, `min_value=0`, `DuplicateBookingPolicyError`→400), `BookingPolicyViewSet(VintaScheduleModelViewSet)` (org-scoped get_queryset; create/update/destroy delegate to DI `booking_policy_service` w/ `set_actor`; idempotent destroy→204), `BookingPolicyPermission` (reads=member, **writes=org admin**), `booking-policies` route, regenerated `schema.yml`. 35 API tests.
- **Review findings fixed**: BLOCKER — permission allowed any member to write org-wide config; gated writes to `IsOrganizationAdmin` (`membership.is_admin`), reads stay member-visible (spec use-case 3). SHOULD-FIX — added `validate_membership_user_id` (bogus id 500→400); deleted dead `get_serializer_context` override; clean `context=` serializer wiring; added member-forbidden(403)/bogus-id(400)/all-four-negative-field tests. Plain ModelSerializer reviewed + cleared.
- **Gates**: ruff clean; mypy no new errors; `makemigrations --check` clean (no migration); `check --deploy` 0 errors; `schema.yml` drift-free; full `pytest -n auto` → 3342 passed.
- **Acceptance**: ✅ REST CRUD org-scoped; duplicate/invalid→400; delete-absent→204; writes audited; member writes forbidden.

### Phase 4 — Policy CRUD public GraphQL ✅
- **Status**: DONE (reviewed, fixed, pushed, PR open)
- **Model used**: sonnet (plan tier T3) — implementer + sonnet fixer
- **Branch**: `plan/bookable-slots-single-calendar-and-booking-policy/phase-4` (base phase-3)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/169
- **Commits**: `5c14975` (impl) + `c0a08ed` (review fixes)
- **Summary**: `BookingPolicyGraphQLType` + input/result types (`calendar_integration/graphql.py`); `booking_policies` query (org-scoped, target filters); `create/update/delete_booking_policy` mutations (`public_api/mutations.py`) delegating to shared `BookingPolicyService` w/ `actor_from_system_user`, org-scoped target resolution, `DuplicateBookingPolicyError`/`ValueError`→GraphQLError, idempotent delete; `PublicAPIResources.BOOKING_POLICY` + 4 `FIELD_TO_RESOURCE_MAPPING` entries. 35 GraphQL tests + 1 service test.
- **Review findings fixed**: BLOCKER — GraphQL bypassed the REST serializer's membership validation → bogus `membership_user_id` 500. Moved membership-existence validation into the **shared service** `create_booking_policy` (raises ValueError; GraphQL maps it) → REST/GraphQL parity in one place. SHOULD-FIX — removed dead `error_message` from both result types (errors-as-exceptions, sibling-consistent); added multiple-targets + duplicate-membership/group GraphQL tests + bogus-membership GraphQL/service tests. NIT — corrected `type: ignore` category on the membership resolver.
- **Gates**: ruff clean; mypy 299 (1 fewer than baseline, no new); `makemigrations --check` clean; `check --deploy` 0 errors; `schema.yml` drift-free; full `pytest -n auto` → 3377 passed.
- **Acceptance**: ✅ GraphQL CRUD org+resource-scoped; behavior matches REST (incl. clean bogus-membership error); writes audited.

### Phase 5 — calendar_bookable_slots single+bundle ✅ (keystone)
- **Status**: DONE (reviewed, fixed, pushed, PR open)
- **Model used**: opus (plan tier T4) — implementer + opus fixer
- **Branch**: `plan/bookable-slots-single-calendar-and-booking-policy/phase-5` (base phase-4)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/170
- **Commits**: `7092937` (impl) + `3e81ddc` (buffer-semantics fix) + `43d81ec` (contract-doc correction)
- **Summary**: New `slot_engine.py` (group walker primitives extracted to pure org-scoped functions + `apply_policy_filter`); `CalendarGroupService.find_bookable_slots` delegates (pure refactor, group tests green, required_count preserved). New `BookableSlotsService.find_bookable_slots_for_calendar` (personal/bundle detect, all-children-free, `resolve_for_calendar`/`resolve_for_bundle`, engine + policy filter), DI `bookable_slots_service`. New `calendar_bookable_slots` query + `PublicAPIResources.BOOKABLE_SLOTS` + owner-scope check. 22 service + 3 GraphQL tests + scenario-#4 test.
- **Review findings fixed**: **BLOCKER (correctness)** — buffer used a candidate-envelope model that was INVERTED vs spec scenario #4 (swapped before/after; first post-event slot 15:10 not 15:20). Corrected to **event-envelope** (dead-zone-around-event): candidate `[start,end]` dropped iff overlaps span expanded to `[bs-buffer_before, be+buffer_after]`; fetch window widened start-by-buffer_after / end-by-buffer_before. **Also corrected the spec + plan buffer-rule wording** (they carried the inverted formula — fixed so Phases 7/8 inherit the right contract). SHOULD-FIX — made the no-policy test compare against the real `CalendarGroupService` reference; added `BookableSlotsValidationError` (was reusing the group exception). NIT — class-level annotation.
- **Gates**: ruff clean; mypy 297 (no new); `makemigrations --check` clean (no migration); `check --deploy` 0 errors; full `pytest -n auto` → 3398 passed; group + single==group parity green (no-policy byte-for-byte holds).
- **Acceptance**: ✅ personal + bundle policy-compliant slots; busy bundle child suppresses; no-policy == pre-feature engine; scenario #4 first post-event slot = 15:20.

## Current phase
Phase 6 — calendar_bookable_slots_with_code (next).

## Remaining phases
- Phase 6 — calendar_bookable_slots_with_code (T2 / haiku)
- Phase 7 — group query policy-aware (T3 / sonnet)
- Phase 8a — enforcement single/bundle/code (T3 / sonnet)
- Phase 8b — enforcement group (T3 / sonnet)

## Deferred phases
None (no cross-repo, no flag-removal — data-presence gate means no flag).
