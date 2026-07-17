# Tracking — Public API Docs (Backend Support)

- **Feature name**: Public API Docs — Backend Support
- **Plan**: `ai-plans/2026-07-16-PUBLIC_API_DOCS_BACKEND_IMPLEMENTATION_PLAN.md`
- **Started**: 2026-07-17
- **Last updated**: 2026-07-17
- **Feature flag**: none — all three phases are purely additive (new URL prefix, new dict, an env-var value change). See the plan's **Risk & Rollout Notes**.

## Run options

| Option | Value |
| --- | --- |
| `pause_between_phases` | false |
| `generate_inline_comments` | false |
| `full_test_suite` | false (scoped suites only) |
| `commit_strategy_resolved` | stacked-branches |
| `use_worktree` | true |
| `worktree_path` | `.claude/worktrees/plan-public-api-docs-backend` |
| `worktree_branch` | `plan-public-api-docs-backend` |
| `worktree_summary` | `.vinta-ai-workflows/worktrees/plan-public-api-docs-backend.yaml` |
| `sandbox_tier` | enforced (claude-hook pre-write guard; no OS-level layer) |

Worktree notes: `node_modules` symlinked; `.env` / `.env.docker` copied with `COMPOSE_PROJECT_NAME=vinta_schedule_api_plan-public-api-docs-backend`; **no DB fork** (the plan introduces no migrations, so `test_db_strategy: fork-on-schema-change` did not trigger — the dev/test DB is shared with the main checkout).

Model assignments: implementer per phase (plan-owned); reviewer tier 3 → `claude-sonnet-5`; fixer tier 2 → `claude-haiku-4-5`; worktree_prep + integrate tier 1 → `claude-haiku-4-5`.

## Completed phases

### Phase 1 — Lock in CORS and introspection for the docs origin ✅

- Base: `plan-public-api-docs-backend`
- Branch: `plan/public-api-docs-backend/phase-1`
- Model: tier 2 → `claude-haiku-4-5` (implementer); reviewer `claude-sonnet-5`; fixer `claude-haiku-4-5`
- Commits: `1510616` (tests + `.env.example`), `a1f6457` (reviewer fix)

Summary:

- Ships **tests only**, exactly as the plan requires. `vinta_schedule_api/settings/base.py` is untouched; the real production/staging origins remain a manual Render env edit (see **Manual follow-ups**).
- New `public_api/tests/test_docs_cors_and_introspection.py` locks three properties: introspection answers on `POST /graphql/` with a non-empty `__schema.types`; a preflight from a configured origin echoes it and permits `authorization`; a preflight from an unconfigured origin gets **no** `Access-Control-Allow-Origin` header at all.
- `.env.example` gained a comment naming the deployed origins; the localhost-only default value is unchanged.
- Reviewer caught a **BLOCKER**: the unconfigured-origin test originally asserted only `allow_origin != evil_origin`. With `CORS_ALLOW_ALL_ORIGINS=True` + `CORS_ALLOW_CREDENTIALS=False`, corsheaders returns a literal `*`, and `"*" != evil` passes — so the test sailed past the exact wildcard misconfiguration it existed to catch. Fixed to `assert allow_origin is None`, which subsumes both "not echoed" and "not wildcard". The fixer proved the teeth: with the wildcard settings applied the test fails on `Got Allow-Origin: '*'`, and passes once reverted.
- Verified independently by the conductor: `check --deploy` clean; `pytest public_api/tests/` 822 passed. mypy has a 322-error pre-existing repo baseline; the new file adds none.

### Infrastructure incident during Phase 1 (resolved)

The worktree was **not** isolated as provisioned. `docker-compose.yml` declares its volumes `external: true` with fixed names, which `COMPOSE_PROJECT_NAME` does not namespace, so the worktree's Postgres mounted the **same** PGDATA as the main checkout's. Two postmasters ran on one data directory (through the implementer's whole test run), and a `docker compose down` in the worktree removed the shared `postmaster.pid`, causing the main checkout's 2-day-old Postgres to self-terminate (`data directory lock file is invalid`). Main's DB was restarted, recovered cleanly (exit 0, no PANIC/corruption), and data was verified intact (77 tables, 21 orgs, 26 calendars).

Resolved by an isolation override kept **outside the repo** at
`<scratchpad>/docker-compose.worktree-noports.yml`, wired in via `COMPOSE_FILE` in the worktree's gitignored `.env`. It forks `dbdata` + `floci_data` to worktree-specific volumes and strips all host port publishing (the compose file hardcodes 5432/5672/6379/8000/1025/8025/4566, which collided with main's running stack). `virtualenv` stays shared on purpose — this plan adds no dependencies.

Also reclaimed 17.1 GB of Docker build cache mid-phase after `No space left on device` (build cache only; volumes were never pruned).

**This is a `prepare-worktree` bug affecting all ~18 worktrees in this repo, not something specific to this plan.** Any worktree that boots its DB stack while the main stack is up reproduces it. Worth fixing upstream in the skill.

### Phase 2 — Serve concept docs over HTTP ✅

- Base: `plan/public-api-docs-backend/phase-1`
- Branch: `plan/public-api-docs-backend/phase-2`
- Model: tier 3 → `claude-sonnet-5` (implementer); reviewer `claude-sonnet-5`; fixer `claude-haiku-4-5`
- Commits: `f1b0fdb` (endpoint), `9cf4b1a` (reviewer fixes), `da6a272` (tuple revert)

Summary:

- `public_api/docs_content.py` globs `docs/concepts/*.md` once at import into a `slug -> Path` allow-list. `get_concept_doc` does `_ALLOWLIST.get(slug)` — a dict key lookup, never a path join — which is the phase's load-bearing security property.
- `PublicApiDocsViewSet` is a plain `ViewSet` (no model, no queryset), `AllowAny`, no auth classes, tagged `Docs`, registered at `public-api-docs` via the `RouteDict` pattern. Modeled on `legal/views.py`'s `PolicyDocumentViewSet` per the plan.
- Serializers are plain `Serializer` subclasses over dicts. `schema.yml` regenerated: +152 lines, only the new endpoints' surface.
- Verified end-to-end by the conductor: all six docs list with real titles; `calendar-groups` markdown is byte-identical to disk; unauthenticated access works; every traversal payload 404s.

**Deviation from the plan (accepted, verified).** The plan specifies `lookup_value_regex = "[a-z0-9-]+"`. That attribute is **inert for routing** here: `vinta_schedule_api/urls.py:28` builds `DefaultRouter(use_regex_path=False)`, which emits `path()`-style routes, and DRF only consults `lookup_value_regex` when `use_regex_path=True`. A `ConceptDocSlugConverter` (regex `[a-z0-9-]+`) is registered as the `docs_slug` path converter instead, and the resolved pattern is confirmed to be `public-api-docs/<docs_slug:slug>/`. `lookup_value_regex` is retained because drf-spectacular reads it to emit `pattern: ^[a-z0-9-]+$` into `schema.yml` (present at `schema.yml:10394`, `:10432`), with a comment at the attribute saying so.

**Reviewer BLOCKER — the traversal tests were vacuous.** The original payloads (`../settings`, `%2Fetc%2Fpasswd`, `../../pyproject`, …) all map to files that do not exist (`../settings.md`, …), so a naive `Path(dir) / f"{slug}.md"` implementation — the exact shape the plan says reviewers must reject — would **also** 404 on every one of them. The suite would have passed straight through the regression it exists to prevent. `docs/concepts/` sits two levels below the repo root, so `../../README` resolves to a real file. Payloads `../../README`, `../../AGENTS`, `../../CODE_OF_CONDUCT` were added. Proven empirically by the conductor: with the naive path-join applied, **all 5 original payloads PASSED and all 3 new payloads FAILED**.

Other fixes this phase: `_extract_title` now strips fenced code blocks before searching, so a doc leading with a shell comment in a ``` fence can't publish a silently-wrong title; the URL converter moved out of a `views.py` import-time side effect into `public_api/converters.py` + `apps.py:ready()`.

**A note on one conductor misstep:** a NIT was pushed to change `authentication_classes` from `()` to `[]` for literal spec conformance, which forced a `# noqa: RUF012` suppression. That was the wrong trade and contradicted repo precedent (`legal/views.py` uses tuples; `calendar_integration/token_views.py:80` uses `tuple()`). Reverted in `da6a272`; ruff is clean without the suppression.

## Current phase

_None — Phase 2 integrating._

## Remaining phases

- **Phase 3** — Serve the webhook event catalog (tier 2 → `claude-haiku-4-5`); base: `plan/public-api-docs-backend/phase-2`

## Deferred phases

None in this repo — the plan declares no feature flag and no cross-repo phase.

## Manual follow-ups (not executed by this run)

1. **Phase 1 deploy step (human).** Append to `CORS_ALLOWED_ORIGINS` on Render — production: `https://schedule.vintasoftware.com`; staging: `https://schedule-staging.vintasoftware.com`. Verify the deployed value after editing: a typo fails open into a broken explorer, visible only in the browser console.
2. **After Phase 3.** Run `amend-plan` against the frontend plan at `~/Workspaces/vinta-schedule-frontend-web/ai-plans/2026-07-16-PUBLIC_API_DOCS_IMPLEMENTATION_PLAN.md` to rewrite its Phase 4 to fetch `/public-api-docs/webhook-events/` instead of hand-authoring the event list.
