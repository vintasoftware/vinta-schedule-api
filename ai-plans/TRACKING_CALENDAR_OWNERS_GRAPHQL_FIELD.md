# Tracking — CALENDAR_OWNERS_GRAPHQL_FIELD

- **Feature**: Add an `owners` field to the Calendar Public GraphQL types.
- **Plan**: `ai-plans/2026-06-18-CALENDAR_OWNERS_GRAPHQL_FIELD_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-06-18
- **Last updated**: 2026-06-19
- **Feature flag**: none (purely additive GraphQL fields).

## Run options
- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: true
- `commit_strategy_resolved`: modular-commits
- `use_worktree`: true
  - `worktree_path`: `.claude/worktrees/plan-calendar-owners-graphql-field`
  - `worktree_branch` / `plan_branch`: `plan/calendar-owners-graphql-field`
  - `worktree_summary`: `.vinta-ai-workflows/worktrees/plan-calendar-owners-graphql-field.yaml`

## Completed phases

### Phase 1 — Expose `owners` on `CalendarGraphQLType` — ✅
- **Model**: claude-sonnet-4-6 (plan tier: Tier 2).
- **Commit**: `feat(public-api): expose owners on Calendar GraphQL type`.
- **Summary**: Added `CalendarOwnershipGraphQLType` (`id` = ownership pk, `is_default`, `user: UserGraphQLType`) and an `owners` method resolver on `CalendarGraphQLType` decorated with `@strawberry_django.field(prefetch_related=["ownerships__user__profile"])`; added the same prefetch to the `calendars` resolver queryset in `public_api/queries.py`. Reused the existing `UserGraphQLType`/`ProfileGraphQLType` from `users/graphql.py`. The declarative-field form failed at runtime (strawberry-django looked for an `owners` attribute on `Calendar`; the related manager is `ownerships`), so a method resolver matching the `CalendarBundleGraphQLType.children` precedent was used — documented deviation.
- **Tests** (`public_api/tests/test_queries.py`, `TestCalendarOwnersField`): (a) shape incl. ownership-pk `id`, `isDefault`, nested `user.profile.{firstName,lastName,profilePicture}`; (b) cross-org no-leak (org-A token sees zero org-B calendar/email/profile); (c) N+1 guard as a two-point comparison — 7 queries for N=1 and N=4 calendars (identical → prefetch confirmed).
- **Review**: Layers 1–3 clean. Reviewer found no BLOCKERs; 4 SHOULD-FIX (hoist late imports ×3, real N=1-vs-N=4 N+1 guard, assert `profilePicture`) all applied by fixer and folded into the phase commit. Branch-name NIT (`plan/` vs `feature/`) intentionally kept — `plan/` is the orchestration convention.
- **Gates**: `ruff check`/`format --check` clean, `manage.py check --deploy` clean (5 expected security.W* warnings), `pytest -n auto` → 2522 passed.
- **Rebased** onto `origin/main` @ c33708f (PR #141) after the upstream tokens-write work added owner-scope helpers to `calendar_integration/graphql.py`; conflict resolved by keeping both the helpers and the new ownership type.

## Current phase
- (about to start) Phase 2 — N+1 hardening for group and bundle entry points.

## Remaining phases
- Phase 2 — N+1 hardening for `calendarGroups` / `calendarBundles` / bundle `children`.
- Phase 3 — Expose `owners` on `CalendarBundleGraphQLType`.

## Deferred phases
- None (no cross-repo phases; no feature flag → no flag-removal phase).
</content>
