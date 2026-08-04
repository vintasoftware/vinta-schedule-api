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
| 2a | Swap allowlist → single redirect | 2 | ✅ done — [PR #207](https://github.com/vintasoftware/vinta-schedule-api/pull/207) | plan/organization-auth-branding/phase-2a |
| 2b | Logo upload and delivery | 3 | ✅ done — [PR #208](https://github.com/vintasoftware/vinta-schedule-api/pull/208) | plan/organization-auth-branding/phase-2b |
| 3 | Widen write gate | 3 | ✅ done — [PR #209](https://github.com/vintasoftware/vinta-schedule-api/pull/209) | plan/organization-auth-branding/phase-3 |
| 4 | Audit + capability field | 2 | ✅ done — [PR #210](https://github.com/vintasoftware/vinta-schedule-api/pull/210) | plan/organization-auth-branding/phase-4 |
| 5 | Resolve branding (parentless) | 3 | ✅ done — [PR #211](https://github.com/vintasoftware/vinta-schedule-api/pull/211) | plan/organization-auth-branding/phase-5 |
| 6 | Invitation reply-to | 2 | ✅ done — [PR #212](https://github.com/vintasoftware/vinta-schedule-api/pull/212) | plan/organization-auth-branding/phase-6 |
| 7 | Post-auth destination server-side | 3 | ✅ done — [PR #213](https://github.com/vintasoftware/vinta-schedule-api/pull/213) | plan/organization-auth-branding/phase-7 |
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

### Phase 2a — Swap allowlist → single redirect_url ✅
- **Branch**: `plan/organization-auth-branding/phase-2a` (base: phase-1) · **PR**: [#207](https://github.com/vintasoftware/vinta-schedule-api/pull/207)
- **Implementer**: sonnet (T2→sonnet) · **Reviewer**: sonnet (T3) · **Fixer**: sonnet (T2)
- `OrganizationBranding.redirect_url` (URLField, blank, default "") replaces `return_url_allowlist` (ArrayField). Migration `0019` (both ops, no backfill).
- New shared `organizations/redirect_url_validation.py` → `validate_redirect_url()`: rejects control chars, non-HTTPS, wildcard, path-prefix, and hostless/malformed (URLValidator schemes=https). Empty "" = no-redirect state (no-op). Used by REST serializer + GraphQL mutation.
- **Deleted `validateReturnUrl`** query + `ValidateReturnUrlResult` type + dead helpers — confirmed zero callers first (the plan's one irreversible step). REST/admin/GraphQL swapped to `redirect_url`.
- **Review caught + fixed a SHOULD-FIX**: GraphQL path (plain `str`, no DRF URLField masking) accepted `https://`, `https:evil.com`, CRLF values → stored open-redirect risk for Phase 7. Fix folded well-formedness + control-char checks into the shared validator so both surfaces enforce identical rules; added GraphQL non-persistence tests + REST wildcard/path-prefix tests.
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4703 passed**. schema.yml + GraphQL surface regenerated.
- Carry-forward: `resolve_branding` (ungated variant) intentionally KEPT but dead-until-Phase-5; docstring fixed to not cite deleted code. `docs/building-blocks-integration-v3.md` still names `validateReturnUrl` in prose (out of scope).

### Phase 2b — Logo upload and delivery ✅
- **Branch**: `plan/organization-auth-branding/phase-2b` (base: phase-2a) · **PR**: [#208](https://github.com/vintasoftware/vinta-schedule-api/pull/208)
- **Implementer**: sonnet (T3) · **Reviewer**: opus (T4) · **Fixer**: sonnet (T2)
- Shared eligibility helper (`organizations/permissions.py`): parentless + `white_label_branding` (+ user-granularity variant for the s3direct `auth` callable). Phase 3 extends this into the full write gate.
- `branding_logos` S3DIRECT destination (private, prefix `uploads/branding_logos/`, PNG/JPEG/WebP, 5 MB, `auth`=eligibility). `OrganizationBranding.logo` (S3DirectImageField) replaces `logo_url`. Migration `0020`.
- Unauthenticated delivery route `GET /branding/logo/<slug>/` via `resolve_branding_for_display`; ETag + short Cache-Control; every miss → default logo identically (no oracle). Default asset `organizations/assets/default_logo.png` (streamed from disk, not S3). GraphQL `createBrandingLogoUpload` signing mutation (org-granularity auth). Reads (serializer/BrandingResult/PublicBrandingResult/email context) return the delivery URL; email uses absolute URL.
- **Tier-4 review caught + fixed TWO BLOCKERs**: (1) cross-tenant object disclosure — unconstrained stored key on a single shared bucket (holds PHI `providers_documents`/`healthcare_entities_documents`); fixed by constraining key to `uploads/branding_logos/` prefix on both writes + defensively on read. (2) stored XSS — extension-driven Content-Type + no nosniff; fixed to allowlisted image types (else `application/octet-stream`) + always `nosniff`. Also normalized a query-count timing oracle.
- **Known residual (documented)**: S3-side size/type enforcement is advisory — s3direct issues bare AWS creds for a client PUT (not a presigned POST), so binding conditions would require rearchitecting a shared surface. Mitigation = prefix constraint + inert delivery.
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4754 passed**. schema.yml regenerated (schema-auth.yml unchanged).
- Carry-forward: `resolve_branding_for_display` now accepts `Organization | None`. `build_logo_delivery_url` keyed by branding ROOT (reseller) org. `BrandingLogoURLField.get_attribute` DRF override (single-instance only; would N+1 in a list). Slug-less root resolves logo_url to `/branding/logo/default/`.

### Phase 3 — Widen write gate ✅
- **Branch**: `plan/organization-auth-branding/phase-3` (base: phase-2b) · **PR**: [#209](https://github.com/vintasoftware/vinta-schedule-api/pull/209)
- **Implementer**: sonnet (T3) · **Reviewer**: opus (T4) · **Fixer**: sonnet (T2)
- `evaluate_branding_write_gate(org)` → `BrandingWriteGateReason` (OK/HAS_PARENT/NOT_ENTITLED/NO_SLUG). Distinguishable refusals per surface. REST: 3 PermissionDenied subclasses (`organizations/exceptions.py`); PUT/PATCH full gate; **GET uses two-condition read gate** (NO_SLUG admitted) so slug-less eligible orgs load the page. GraphQL `updateBranding` swaps `assert_org_can_invite` for the gate; `UpdateBrandingInput.slug` optional → sets slug+branding in ONE `transaction.atomic()` call (atomic rollback verified vs graphql-core's HTTP-200 swallow). Admin form `clean` refuses parented org.
- Two-condition logo-signing helper (Phase 2b) kept separate — logo upload does NOT require a slug.
- No migration (no model change). Reseller fixtures now need a slug (asserted).
- **Tier-4 review lead finding (fixed)**: GET was over-gated on slug → would 403 slug-less eligible orgs and pre-break Phase 4's `can_manage_branding` contract; fixed to two-condition read gate. Also fixed: friendly error (not IntegrityError 500) on GraphQL slug-collision race via nested savepoint. Verified holding: no gate bypass on any write surface, atomicity, no reason leak.
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors (+ fixed a latent User|None narrowing at 2 touched sites), **full suite 4778 passed**. schema.yml regenerated (only 403 descriptions changed).
- Carry-forward: `can_manage_branding` (Phase 4) = parentless+entitled (NO slug) — must equal GET-reachability. Out-of-scope flagged: pre-existing `S3DirectWidget`/admin-form required-when-blank + re-render crash (worked around in tests).

### Phase 4 — Audit + capability field ✅
- **Branch**: `plan/organization-auth-branding/phase-4` (base: phase-3) · **PR**: [#210](https://github.com/vintasoftware/vinta-schedule-api/pull/210)
- **Implementer**: sonnet (T2) · **Reviewer**: sonnet (T3) · **Fixer**: sonnet (T2)
- Audit on branding create (no diff) + update (diff of changed fields) via `AuditService.record` on REST PUT/PATCH + GraphQL `updateBranding`; actor `actor_from_membership` (REST) / `actor_from_system_user` (partner API); refused writes record nothing (audit call unreachable + `on_commit` defer). Shared `branding_diff_state()` helper (6 fields; logo→key).
- `can_manage_branding` read-only on `CurrentMembershipSerializer` + `MyMembershipSerializer` = `is_branding_eligible_organization` (parentless+entitled, NO slug) → equals GET-reachability. No migration.
- **Review (no BLOCKER)**: fixed N+1 on `/organizations/mine/` (new `EntitlementService.has_entitlement_for_organizations` bulk helper + `_MyMembershipListSerializer` batching; query-count test proves no linear scaling); hoisted late audit imports (no cycle).
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4800 passed**. schema.yml regenerated.
- Carry-forward: bulk helpers `EntitlementService.has_entitlement_for_organizations` + `is_branding_eligible_organizations` now exist. 3 redundant local `AuditService` imports remain in unrelated booking-policy mutations (out-of-scope cleanup).

### Phase 5 — Resolve branding for parentless orgs ✅
- **Branch**: `plan/organization-auth-branding/phase-5` (base: phase-4) · **PR**: [#211](https://github.com/vintasoftware/vinta-schedule-api/pull/211)
- **Implementer**: sonnet (T3) · **Reviewer**: opus (T4) · **Fixer**: sonnet (T2)
- `get_branding_root()`: reseller ancestor first (unchanged) → else `self` when parentless → else `None`. Parentless non-reseller now resolves to itself; child under non-reseller parent still `None` (fallback keys on `self.parent_id`, not the walk var → no leak to children). `branding_for_tenant` gains slug lookup (id precedence; all miss modes → default indistinguishably, body-level). `notification_contexts.py` unchanged (already flows through widened root). No migration.
- **Review (no BLOCKER)**: root matrix verified on every flowchart branch, no cross-org leak, downgrade gate fires for new self-root. Added tests pinning brandingForTenant id-vs-slug tie-break.
- **⚠️ ACCEPTED TRADE-OFF (supersedes plan L57/L325 "same path / not an existence oracle" claim)**: widening the root reopens a **1-query timing divergence** on the unauthenticated logo route + brandingForTenant — a real parentless org runs one entitlement query an unknown slug doesn't. Response **body/status/headers stay byte-identical**; only server-side query-count/timing differs → timing-only, not reliably weaponizable over the network. Hard close (phantom query on every anon cache-miss) not worth it; a child already pays a parent-walk query, so found≠not-found in query count is unavoidable once branding widens past resellers. **Accepted; not fixed.** The Phase 2b oracle test now asserts "exactly one expected extra query" (still catches N+1 regressions).
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4818 passed**. schema.yml regenerated (incl. a benign Phase-4 `can_manage_branding` docstring hunk that Phase 4 never regenerated).

### Phase 6 — Invitation reply-to ✅
- **Branch**: `plan/organization-auth-branding/phase-6` (base: phase-5) · **PR**: [#212](https://github.com/vintasoftware/vinta-schedule-api/pull/212)
- **Implementer**: sonnet (T2) · **Reviewer**: sonnet (T3) · **Fixer**: sonnet (T2)
- New `ReplyToDjangoEmailNotificationAdapter` (subclasses vintasend stock adapter; DI-wired in `di_core/containers.py`) — reply-to concept did NOT exist in the send path, this is the plumbing. `organization_invitation_context` exposes resolved `support_email` (widened root, entitlement-gated) as `reply_to`. From provably unchanged (`NOTIFICATION_DEFAULT_FROM_EMAIL`); no reply-to verification (Open Q1 out of scope). No migration.
- **Review (no BLOCKER)**: fixed scope creep — first pass stamped `Reply-To: <from>` on EVERY email through the shared adapter (dunning/usage-warning/password-reset/calendar); narrowed to only-branded-invitations (`reply_to` set only when context supplies it; else `[]`, byte-identical to today). Drift-guard comment names reviewed base version `vintasend-django==1.2.1`.
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4826 passed**. No schema change.

### Phase 7 — Post-auth destination server-side ✅
- **Branch**: `plan/organization-auth-branding/phase-7` (base: phase-6) · **PR**: [#213](https://github.com/vintasoftware/vinta-schedule-api/pull/213)
- **Implementer**: sonnet (T3) · **Reviewer**: opus (T4) · **Fixer**: sonnet (T2)
- `ProviderCallbackAPIView` (allauth headless) merges a `destination` into the JSON response: configured `redirect_url` when entitled+set, else dashboard (`FRONTEND_BASE_URL`). Resolved ONLY from `request.user` + DB branding (`resolve_branding_for_display`), never `state`/query/header. `state["next"]` still = OAuth `client.callback_url` (token exchange) but no longer decides landing. Structured log (`organization_id` + `destination_source`, URL not logged). In-place response body rewrite (preserves cookies/headers). No migration; no 302 route.
- New setting `FRONTEND_BASE_URL` in `base.py` + `.env.example` + `.env.docker.example` (was staging/production only).
- **Review (no BLOCKER)**: open-redirect verdict CLEAN by full trace (custom dispatch skips tenant-scoping so even `X-Organization-Id` can't steer org). Fixed: type hint, env-example docs, in-place response rewrite. Open-redirect guards (`state["next"]=https://evil.example` → destination unaffected on branded + fallback paths) pass.
- Gates: ruff/format clean, `makemigrations --check` no changes, `manage.py check` clean, mypy no new errors, **full suite 4833 passed**.

## Current phase

Phase 8 — Branded login by organization slug.

## Deferred phases

_(none — no cross-repo or flag-removal phases in this plan)_
