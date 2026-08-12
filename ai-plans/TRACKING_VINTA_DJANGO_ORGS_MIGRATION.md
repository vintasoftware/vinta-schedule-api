# Tracking — Vinta Django Orgs Migration

- **Feature**: Migrate the `organizations` app onto `vinta-django-orgs`; replace `role` / `is_billing_owner` with the package's groups + permissions.
- **Plan**: [ai-plans/2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md](2026-08-12-VINTA_DJANGO_ORGS_MIGRATION_IMPLEMENTATION_PLAN.md)
- **Plan id**: `VINTA_DJANGO_ORGS_MIGRATION` (kebab: `vinta-django-orgs-migration`)
- **Started**: 2026-08-12
- **Last updated**: 2026-08-12
- **Feature flag**: none — the plan's Guiding Decisions justify the omission (no flag module in this repo; an app rename and a default-manager swap resolve at class-definition and migration time and cannot be gated per-request). There is consequently no flag-removal phase.

## run_options

| Option | Value | Source |
|---|---|---|
| `pause_between_phases` | `false` | config default |
| `generate_inline_comments` | `false` | config default |
| `full_test_suite` | `false` (scoped) | config default |
| `use_worktree` | `true` | config default |
| `commit_strategy_resolved` | `stacked-branches` | asked (config = `ask`) |
| `worktree_path` | `.claude/worktrees/plan-vinta-django-orgs-migration` | prepare-worktree |
| `worktree_branch` | `plan-vinta-django-orgs-migration` | prepare-worktree |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-vinta-django-orgs-migration.yaml` | prepare-worktree |
| `sandbox_tier` | `enforced` (`sandbox-exec`) | probed |

**agent_models**: reviewer Tier 3, fixer Tier 2, worktree_prep Tier 1, integrate Tier 1. Per-phase `**Review models**:` overrides in the plan win over these for reviewer/fixer.

## WORKROOT

- `WORKROOT` = `/Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-vinta-django-orgs-migration`
- `BASE_BRANCH` = `plan-vinta-django-orgs-migration` (based on `origin/main` @ `c013f2a`)
- `SANDBOX_TIER` = `enforced`

### Worktree provisioning corrections

The Tier 1 `worktree_prep` delegate reported success but the worktree was not runnable. Four defects were found and fixed by the conductor before any phase started. Recorded here because they are provisioning-level facts a resumed run must not re-break:

1. **`vinta_schedule_api/settings/local.py` was missing.** It is gitignored, so the worktree checkout did not carry it and Django could not load at all on the host surface (`ModuleNotFoundError: vinta_schedule_api.settings.local`). Copied from the main checkout.
2. **Host-surface `.env` pointed at the MAIN dev database.** `.env.docker` (container surface) was correctly forked, but `.env` — which the host surface and every pre-commit hook read — still named `vinta_schedule_api`. Given this plan runs a primary-key change and column drops, that would have applied destructive DDL to the main dev DB. Repointed to `vinta_schedule_api_wt_plan_vinta_django_orgs_migration`.
3. **The compose override's port stripping was a no-op.** It used `ports: []`, but Compose merges list fields by appending, so every host port stayed published and the stack collided with the main checkout's (observed: `Bind for 0.0.0.0:1025 failed`). Rewritten to `ports: !reset []`. The override also omitted the `mailpit` and `floci` services entirely; both added.
4. **The override was wired via `COMPOSE_FILE` in `.env.docker`, which `docker compose` does not auto-read** (it reads `.env`). The override was therefore never applied and the project name fell back to the directory name. Moved `COMPOSE_PROJECT_NAME` + `COMPOSE_FILE` into the worktree's `.env`.
5. **The two surfaces pointed at two different postgres servers.** The worktree's compose stack has its own postgres (namespaced `dbdata` volume), but the forked dev DB had been created in the *main checkout's* postgres, and stripping every host port left the host surface unable to reach the worktree's own server at all. `db` now publishes a distinct host port (`5433`, via `ports: !override ["5433:5432"]` — note `!reset` discards the value it is given, so it cannot be used to *replace* a list), the dev DB was created inside the worktree's postgres, and `.env` points the host surface at `localhost:5433`. Both surfaces now resolve `current_database() = vinta_schedule_api_wt_plan_vinta_django_orgs_migration` on the same isolated server.

Verified after fixing: host surface and container surface both load Django and both resolve `NAME = vinta_schedule_api_wt_plan_vinta_django_orgs_migration`; `docker compose config` publishes no host ports; the main checkout's stack is still running and its working tree is untouched.

## Phases

10 phases across 7 layers. No cross-repo phases. No flag-removal phase.

| Phase | Title | Impl tier | Review override | Status | Branch | Base | PR |
|---|---|---|---|---|---|---|---|
| 0 | Bind the organization at every unscoped call site | 3 | reviewer 4 | ✅ done | `plan/vinta-django-orgs-migration/phase-0` | `plan-vinta-django-orgs-migration` | see below |
| 1a | Install the package and rename our app to `tenancy` | 2 | — | ⏳ pending | — | phase-0 | — |
| 1b | Move the migration graph, content types, swappable settings | 3 | reviewer 4 | ⏳ pending | — | phase-1a | — |
| 1c | Unwind the composite PK, subclass abstract bases, backfill slugs | 4 | reviewer 4, fixer 3 | ⏳ pending | — | phase-1b | — |
| 2a | Flip `calendar_integration` onto the mixin and safe relations | 4 | reviewer 4 | ⏳ pending | — | phase-1c | — |
| 2b | Flip the remaining scoped models | 3 | reviewer 4 | ⏳ pending | — | phase-2a | — |
| 3 | Groups, permissions, and the organization auth backend | 3 | — | ⏳ pending | — | phase-2b | — |
| 4 | Migrate the permission classes to `has_perm` | 4 | reviewer 4 | ⏳ pending | — | phase-3 | — |
| 5 | Expose permissions on REST and GraphQL, drop `role` | 3 | — | ⏳ pending | — | phase-4 | — |
| 6 | Drop `role` / `is_billing_owner` and delete the old tenancy layer | 1 | — | ⏳ pending | — | phase-5 | — |

## Completed phases

### Phase 0 — Bind the organization at every unscoped call site

- **Branch**: `plan/vinta-django-orgs-migration/phase-0` off `plan-vinta-django-orgs-migration`
- **Commits**: `625ca10` (implement, Tier 3) · `fb6012f` (review fixes, fixer Tier 2)
- **Review**: reviewer Tier 4 (per the plan's `**Review models**:` override). **No BLOCKERs.** Six SHOULD-FIX and three NITs applied; two NITs deliberately skipped as churn.
- **Gate**: ruff clean, `makemigrations --check` reports no changes, scoped suite **3455 passed / 0 failed**. Full suite 5434 passed.

What landed: `common/organization_context.py` (a local `contextvars` implementation mirroring the package's `organizations.state` API name-for-name, so Phase 1a/2 swaps it for a re-export in one file); organization bindings in the four Celery task modules and five management commands, per-iteration at every fan-out site; `original_manager` made explicit on the two deliberately cross-organization scans (`organizations/admin.py` documented, `refresh_webhook_subscriptions.py` changed); and the `assert_no_unbound_scoped_queries` tripwire wired into six real test files.

Carry-forward facts for later phases:

- **The neutrality claim now has one qualification.** Fixing the lazy-`None` binds means `webhooks/tasks.py` and `audit/tasks.py` each run one real `Organization` lookup that Phase 0 previously never executed (the `SimpleLazyObject` was never forced). Deliberate, and the cost of failing at the task boundary instead of deep inside Phase 2's manager — but this phase is not literally zero-overhead.
- **`process_webhook_event` has no test that executes its body.** `webhooks/tests/test_services.py` patches `.delay`/`.apply_async` and asserts dispatch only. Pre-existing gap, not introduced here — but it means the webhooks binding is the one Phase 0 change with no execution coverage. Worth closing before Phase 2b flips `webhooks` models.
- **The neutrality test cannot request the tripwire.** `test_bound_and_unbound_runs_produce_identical_observable_outcomes` deliberately runs one arm unbound, so the tripwire fails there by design. Documented in the test rather than silenced.
- **Tripwire blind spot.** It guards seven queryset entry points (`__iter__`, `get`, `count`, `exists`, `update`, `delete`, `aggregate`). Any custom manager method that iterates independently still slips past. Phase 2a should close this.
- **`payments` pooled-subtree reads stay uncovered.** `MeteringService.expand_occurrence_identities` reads across a subscription's entire reseller subtree via `organization_id__in=...`, which no single-organization binding can cover. Consistent with the plan's **Open Questions** row on payments scoping; Phase 2a owns the `original_manager` decision.

**Verification note for every later phase:** `pytest.ini` sets a 10-second per-test timeout. Running any two test suites (or a test suite alongside a file-scanning subagent) concurrently causes unrelated tests to fail spuriously with `Failed: Timeout (>10.0s)`. Several Phase 0 "failures" were traced to exactly this. Run verification serially on an otherwise idle machine, and re-run any failure alone before believing it.

## Current phase

Phase 1a — not yet started.

## Deferred phases

_(none — no cross-repo phases, no flag-removal phase)_
