# Tracking — Organization Auth-Area Branding

- **Plan**: [ai-plans/2026-08-04-ORGANIZATION_AUTH_BRANDING_IMPLEMENTATION_PLAN.md](2026-08-04-ORGANIZATION_AUTH_BRANDING_IMPLEMENTATION_PLAN.md)
- **Spec**: [ai-plans/2026-08-04-ORGANIZATION_AUTH_BRANDING_SPEC.md](2026-08-04-ORGANIZATION_AUTH_BRANDING_SPEC.md)
- **Started**: 2026-08-04
- **Last updated**: 2026-08-04
- **Feature flag**: none (additive; see plan Guiding Decisions)

## Run options

- `pause_between_phases`: false (auto-flow)
- `generate_inline_comments`: false
- `full_test_suite`: false (scoped tests per phase)
- `commit_strategy_resolved`: stacked-branches
- `use_worktree`: **false** — forced. Docker registry is blocked by the environment egress
  policy, so `prepare-worktree` (docker-compose + docker-forked DBs) cannot run. Executing in
  the main checkout instead.
- `base_branch`: `main` (@ ed63753, which already contains the spec commit)

## Execution surface — HOST, not container

Docker Hub image pulls are 403'd by the egress policy (`production.cloudfront.docker.com`
blob CDN blocked). The container surface (`docker compose run --rm api …`) is therefore
unavailable. **All gates run on the host surface**, which is what the project's pre-commit
and CI use. Host Postgres 16 cluster is running; role+db `vinta_schedule_api` created;
`.env` and `vinta_schedule_api/settings/local.py` created from their `.example` templates.

Command translation (container → host):

| Purpose | Host command |
|---|---|
| Lint | `uv run ruff check ./` |
| Format | `uv run ruff format ./` (check: `--check`) |
| Build | `uv run python manage.py check --settings=vinta_schedule_api.settings.test` |
| Migrations gate | `uv run python manage.py makemigrations --check --settings=vinta_schedule_api.settings.test` |
| Make migrations | `uv run python manage.py makemigrations <app> --settings=vinta_schedule_api.settings.test` |
| Scoped tests | `uv run pytest <app>/tests/ -n auto` |
| Full tests | `uv run pytest -n auto` |
| Types | `uv run mypy <paths>` (baseline: 146 pre-existing errors across 18 files; judge new errors only) |
| Schema regen | `uv run python manage.py spectacular --color --file schema.yml --settings=vinta_schedule_api.settings.test` |

`pytest.ini` pins `--ds=vinta_schedule_api.settings.test`; test settings need only Postgres
(Celery eager, Redis empty/optional, email locmem, storage filesystem).

## Agent models

- Implementer: per-phase `Suggested AI model` tier.
- Reviewer: `agent_models.reviewer` = tier 3 (sonnet); phase override to tier 4 (opus) on 2b/3/5/7.
- Fixer: `agent_models.fixer` = tier 2 (sonnet).
- Tier map (Anthropic): T1→haiku, T2→sonnet, T3→sonnet, T4→opus.

## Phases

| # | Title | Tier | Status | Branch |
|---|---|---|---|---|
| 1 | Self-serve organization slug | 3 | ✅ done — [PR #206](https://github.com/vintasoftware/vinta-schedule-api/pull/206) | plan/organization-auth-branding/phase-1 |
| 2a | Swap allowlist → single redirect | 2 | ⏳ pending | plan/organization-auth-branding/phase-2a |
| 2b | Logo upload and delivery | 3 | ⏳ pending | plan/organization-auth-branding/phase-2b |
| 3 | Widen write gate | 3 | ⏳ pending | plan/organization-auth-branding/phase-3 |
| 4 | Audit + capability field | 2 | ⏳ pending | plan/organization-auth-branding/phase-4 |
| 5 | Resolve branding (parentless) | 3 | ⏳ pending | plan/organization-auth-branding/phase-5 |
| 6 | Invitation reply-to | 2 | ⏳ pending | plan/organization-auth-branding/phase-6 |
| 7 | Post-auth destination server-side | 3 | ⏳ pending | plan/organization-auth-branding/phase-7 |
| 8 | Branded login by slug | 2 | ⏳ pending | plan/organization-auth-branding/phase-8 |
| 9 | Client handoff (SPA) | 1 | ⏳ pending | plan/organization-auth-branding/phase-9 |

## Completed phases

### Phase 1 — Self-serve organization slug ✅
- **Branch**: `plan/organization-auth-branding/phase-1` (base: `main`) · **PR**: [#206](https://github.com/vintasoftware/vinta-schedule-api/pull/206)
- **Implementer**: sonnet (T3) · **Reviewer**: sonnet (T3) · **Fixer**: sonnet (T2)
- Added `Organization.slug` (unique, nullable, indexed; `default=None` so multiple unset orgs coexist as NULL). Migration `0018_organization_slug`.
- New `organizations/slug_validation.py` — single shared `validate_organization_slug()`: reserved words (live routes `super`/`schema`/`s3direct`/`auth`/`api`/… + vendor variants), format/length (lowercase alnum + internal hyphen, 3–63, not purely numeric), confusables (stdlib `unicodedata`, no new dep — rejects non-ASCII/mixed-script lookalikes).
- REST: `slug` writable on `OrganizationSerializer` as **CharField** (shared validator is sole authority; collision → 400 naming conflict), read-only on `OrganizationBriefSerializer`. Admin: `slug` as `forms.CharField` + searchable.
- **Review caught + fixed a BLOCKER**: `SlugField`'s auto ASCII regex preempted the custom validator on both REST + admin, making confusables dead code. Fixed by switching to CharField/forms.CharField (model field unchanged, no extra migration); added Cyrillic-homoglyph tests through PATCH + admin form.
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4707 passed**. schema.yml regenerated.
- Carry-forward for later phases: GraphQL `updateBranding` (Phase 3) sets slug in one call; REST org *creation* does not accept slug today (only PATCH) — a symmetric create path was left out of scope per acceptance criteria.

## Current phase

Phase 2a — Swap the allowlist for a single redirect destination.

## Deferred phases

_(none — no cross-repo or flag-removal phases in this plan)_
