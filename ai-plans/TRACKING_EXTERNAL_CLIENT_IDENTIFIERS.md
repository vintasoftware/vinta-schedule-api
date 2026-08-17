# Tracking — External Client Identifiers

- **Feature**: External Client Identifiers
- **Plan**: `ai-plans/2026-08-17-EXTERNAL_CLIENT_IDENTIFIERS_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-08-17
- **Last updated**: 2026-08-17
- **Feature flag**: none. The plan's "No feature flag" decision stands in a
  no-op-when-omitted regression test on every phase that touches an existing write path.

## Run options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config default (auto-flow) |
| `generate_inline_comments` | `false` | config default |
| `full_test_suite` | `false` (scoped) | user answer |
| `commit_strategy_resolved` | `stacked-branches` | user answer |
| `use_worktree` | `true` | config default |
| `worktree_path` | `.claude/worktrees/plan-external-client-identifiers` | prepare-worktree |
| `worktree_branch` | `plan-external-client-identifiers` | prepare-worktree |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-external-client-identifiers.yaml` | prepare-worktree |
| `sandbox_tier` | `enforced` (not wrappable — see below) | prepare-worktree probe |

**Sandbox caveat.** `sandbox-exec` is available, but this runtime spawns subagents
in-process, so the per-spawn `sandbox-run.sh` wrap does not apply. Main-checkout
write prevention falls back to the review-phase stray-write check: the conductor runs
`git -C <main_checkout> status --short` after every implementer and every fixer.

**Worktree fixes applied before Phase 1** (both were provisioning gaps that would
have failed every phase):

1. `virtualenv` compose volume was left shared with the main checkout, so `uv run`
   died with `failed to remove directory /opt/venv: Permission denied`. Forked it to
   `vinta-schedule_wt_plan-external-client-identifiers_virtualenv`, matching
   `shared_volumes: []` in `.vinta-ai-workflows.yaml`.
2. `vinta_schedule_api/settings/local.py` is gitignored and was not copied, so the
   `backend-schema-local` pre-commit hook raised `ModuleNotFoundError` on every
   commit. Copied it in.

Verified after the fixes: `migrate` applies cleanly and `pytest` collects 2042 tests.

## Model assignments

Implementer models come from each phase's `**Suggested AI model**:` line; reviewer /
fixer / integrate come from `agent_models` in `.vinta-ai-workflows.yaml`. Only the
anthropic vendor is exposed by this runtime.

| Role | Tier | Model |
|---|---|---|
| Phase 1 implementer | 2 (stepped up — touches 6 files) | `claude-sonnet-5` |
| Phase 2 implementer | 3 | `claude-sonnet-5` |
| Phase 3 implementer | 3 | `claude-sonnet-5` |
| Phase 4 implementer | 2 (stepped up — touches 5 files) | `claude-sonnet-5` |
| Phase 5 implementer | 3 | `claude-sonnet-5` |
| Phase 6 implementer | 3 | `claude-sonnet-5` |
| Reviewer (default) | 3 | `claude-sonnet-5` |
| Reviewer (Phase 6 override) | 4 | `claude-opus-4-8` |
| Fixer | 2 | `claude-haiku-4-5` |
| Integrate | 1 | `claude-haiku-4-5` |

## Completed phases

### Phase 1 — Add the ExternalClientIdentifier table ✅

- **Branch**: `plan/external-client-identifiers/phase-1` (base: `plan-external-client-identifiers`)
- **Implementer**: `claude-sonnet-5` (plan Tier 2, stepped up — 6 files)
- **Reviewer**: `claude-sonnet-5` (Tier 3) — 0 BLOCKER, 4 SHOULD-FIX, 2 NIT
- **Fixer**: `claude-haiku-4-5` (Tier 2)
- **Commits**: `61f7cf0b` (table), `fa0dfc89` (normalize_system fixes)
- **Diff**: 6 files, +575

Shipped the `ExternalClientIdentifier` table exactly as **Data Model Changes** specifies:
`SingleOrganizationModelMixin` + `SafeRelationNullInitMixin` + `BaseModel`, a
`content_type` / `identified_key` generic FK pair, both unique constraints
(`extclientid_uniq_target_system`, `extclientid_uniq_system_ident`) and the lookup index
(`extclientid_org_ct_key_idx`). `GenericRelation` on `CalendarEvent` and
`ExternalAttendee` gives the cascade, including the parent-driven case
(`Calendar` → `CalendarEvent` → identifiers) that a `post_delete` signal would miss.
Also: `IDENTIFIABLE_MODELS` + `normalize_system`, an org-scoped factory, and an editable
admin whose `clean_system` normalizes.

`makemigrations` folded the constraints and indexes into a single `CreateModel`, which is
Django's normal output for a brand-new table (precedent: `0039_bookingpolicy.py`). No
`AddIndexConcurrently` and no `atomic = False`, as the phase requires.

**Fixes applied from review** (all in `normalize_system`, the function both unique
constraints depend on):

- It was **not idempotent**. `https://crm.example.com//` normalized to
  `https://crm.example.com/`, which normalized again to `https://crm.example.com` — so one
  logical system could occupy two rows, defeating the uniqueness the feature is built on.
  Now strips all trailing slashes.
- `netloc.lower()` **mangled userinfo**, lowercasing credentials. Now lowercases only the
  host. Verified live: `https://User:Pass@CRM.Example.com/api` →
  `https://User:Pass@crm.example.com/api`; port, path, query and fragment case all preserved.
- Tests for both, plus an idempotency test. 27 tests in the file.

**Verification** (all re-run by the conductor inside the worktree):
`ruff check` clean · `ruff format --check` 642 files · `makemigrations --check` no changes ·
`check --deploy` 0 errors (5 pre-existing dev warnings) · `mypy` 291 pre-existing errors,
0 new, new module clean · **`pytest calendar_integration/tests/` → 2069 passed, 0 failed**
(2042 baseline + 27) · migration **reverses and reapplies** cleanly.

**Declined from review — admin `get_queryset` uses the org-scoped manager.** The reviewer
proposed switching to `original_manager`, citing `audit/admin.py`. Not applied, for three
reasons: the plan's **Risk & Rollout Notes** call a new read path reaching for
`original_manager` a blocker; `AuditAdmin`'s bypass is justified by its being read-only
(all three permission methods return `False`) whereas this admin is editable by plan
decision, so the same bypass would expose cross-organization editing; and it is not a
regression — all 11 `get_queryset` overrides in `calendar_integration/admin.py` behave
this way and none use `original_manager` (only `organizations/` and `audit/` do). Tracked
as a follow-up below.

**NITs not actioned**: `extclientid_uniq_target_system` is exactly 30 characters, Django's
limit — it applies fine, but leaves no headroom for a future rename. `normalize_system`'s
docstring still says "strip a trailing slash" (singular) and does not mention userinfo
handling; it matches the plan's own wording but now understates the behavior.

## Current phase

**Phase 2 — Identifier write service, normalization, validation, audit**
- Branch: `plan/external-client-identifiers/phase-2`
- Base: `plan/external-client-identifiers/phase-1`
- Status: not started

## Follow-ups (not blocking this plan)

1. **`calendar_integration/admin.py` org-scoping under `STRICT_ORGANIZATION_FILTER`.** All
   11 admins in that file call `super().get_queryset(request)`, which resolves to the
   org-scoped default manager. With no organization bound to a staff request, the
   changelist raises `OrganizationNotFoundError` (500) rather than listing rows.
   Pre-existing and repo-wide within that file; deserves one uniform fix across all 11,
   not a per-model bypass. `audit/admin.py` documents the failure mode in detail.

## Remaining phases

| Phase | Title | Depends on |
|---|---|---|
| 2 | Identifier write service, normalization, validation, audit | 1 |
| 3 | Public GraphQL read, create-time write, and lookup | 1, 2 |
| 4 | Carry event identifiers in webhook payloads | 1, 2 |
| 5 | Internal REST read, write and filtering | 1, 2 |
| 6 | Public `updateCalendarEvent` mutation | 1, 2, 3 |

## Deferred phases

None. The plan declares no cross-repo phase and no feature flag, so there is no
flag-removal phase to defer.
