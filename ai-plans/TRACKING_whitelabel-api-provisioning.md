# Tracking — White-Label API Provisioning

- **Feature**: White-Label API-Only Provisioning
- **Plan**: ai-plans/2026-06-16-WHITELABEL_API_PROVISIONING_IMPLEMENTATION_PLAN.md
- **Plan id**: whitelabel-api-provisioning
- **Started**: 2026-06-16
- **Last updated**: 2026-06-17

## Run options
- pause_between_phases: false
- generate_inline_comments: true
- use_worktree: false
- commit_strategy_resolved: stacked-branches
- pr_creation: agents-create

## Feature flag
None (capability switch `can_invite_organizations`, DB-only, default off — not a rollout flag). No flag-removal phase.

## Completed Phases

### Phase 0 — Org hierarchy, capability flag, gate helper ✅
- **Status**: merged-ready, PR #85 (https://github.com/vintasoftware/vinta-schedule-api/pull/85)
- **Branch**: plan/whitelabel-api-provisioning/phase-0 (base: main)
- **Model**: claude-haiku-4-5 (plan Tier 2) · agent migration-author
- **Commits**: 5f26c44 (feat) + 7de22ca (fix guard) + 04e035d (fix cycle/admin)
- **Summary**: Added `Organization.parent` self-FK (`on_delete=PROTECT`) + DB-only `can_invite_organizations` boolean (`default=False`); `is_reseller()`; `get_branding_root()` walks parent chain to nearest reseller ancestor, with a visited-PK cycle guard (no hang on parent cycles), returns None when no reseller. Added `public_api/capabilities.py::assert_org_can_invite` raising DRF `PermissionDenied`. Added MEMBERSHIP/INVITATION/BRANDING/CHILD_ORG_ANALYTICS to `PublicAPIResources` (+ ResourceAccess choices migration + schema.yml regen). Org admin exposes the flag (only toggle surface). Blocker-class guard test introspects `public_api/schema.py::schema._schema.type_map` (all input+output types) with anti-vacuity assertions; serializer scan hardened (ModuleNotFoundError-only swallow, asserts OrganizationSerializer scanned).
- **Gate**: 1888 passed; check --deploy clean; makemigrations --check clean.
- **Review**: Layer 3 found 1 BLOCKER (vacuous GraphQL guard) → fixed; SHOULD-FIX (cycle guard, serializer-guard vacuity, admin scope creep) → fixed.
- **Carry-forward for later phases**:
  - Gate helper signature: `assert_org_can_invite(acting_org) -> None` raises `rest_framework.exceptions.PermissionDenied`. Call it AFTER the ResourceAccess scope check in every bundle resolver.
  - Branding resolution entry point: `org.get_branding_root() -> Organization | None` (None ⇒ vinta default). **Deferred perf note**: walk is lazy FK, one query per hop (N+1 on deep chains) — Phase 6 (`resolve_branding`) should add `select_related` / depth cap if needed.
  - New scopes already in `PublicAPIResources`; map fields to them via `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` in each phase.

### Phase 1 — createOrganization (gated child provisioning) ✅
- **Status**: PR #86 (https://github.com/vintasoftware/vinta-schedule-api/pull/86), base phase-0
- **Branch**: plan/whitelabel-api-provisioning/phase-1
- **Model**: claude-haiku-4-5 (Tier 2) · agent implementer
- **Commits**: 3c5cdfc (feat) + ea80bf4 (fix race + harden tests)
- **Summary**: `createOrganization(input:{name})` GraphQL mutation. Dual gate — `OrganizationResourceAccess('ORGANIZATION')` permission class (pre-resolver scope) + `assert_org_can_invite(acting_org)` (in-resolver DB flag). Child created with `parent=acting_org`, `can_invite_organizations=False` hardcoded (never from input). Acting org from `request.public_api_organization`. Dup sibling name blocked by `UniqueConstraint(parent,name)` (migration 0009, NULL-distinct) + IntegrityError backstop + friendly message. No membership created.
- **Gate**: 1896 passed; check --deploy + makemigrations --check clean.
- **Review**: no BLOCKER; SHOULD-FIX (TOCTOU race → unique constraint; weak security asserts → specific gate/scope message + no-creation; flag-injection test behavioral) all fixed.
- **Carry-forward**: gated-public-mutation pattern — register field in `FIELD_TO_RESOURCE_MAPPING` (public_api/permissions.py), permission_classes=[IsAuthenticated, OrganizationResourceAccess], acting org via `info.context.request.public_api_organization`, `assert_org_can_invite` in-resolver after scope. Types in public_api/types.py. organizations migrations now at 0009.

### Phase 3 — createInvitation (branded-email path) ✅
- **Status**: PR #88 (https://github.com/vintasoftware/vinta-schedule-api/pull/88), base phase-2
- **Branch**: plan/whitelabel-api-provisioning/phase-3
- **Model**: claude-sonnet-4-6 (Tier 3) · implementer (2 socket deaths mid-run; orchestrator finished enum/lint + drove fixer)
- **Commits**: f181281 (single — feat, includes orchestrator enum/lint recovery + fixer BLOCKER/test work)
- **Summary**: `createInvitation(input:{userEmail,organizationId,role=MEMBER,sendEmail=true})` → `{invitation{id email expiresAt}, token, inviteUrl}` (token/url null — email path only; sendEmail=false is Phase 4). Gate INVITATION + assert_org_can_invite. `OrganizationInvitation` gains `role` (default MEMBER); `invited_by` now nullable+SET_NULL (caller is SystemUser). migration 0010. Role propagates on accept via BOTH `accept_invitation` (token) and `provision_tenant_for_user` (social). Already-active-member → UserAlreadyHasMembershipError.
- **Gate**: 1923 passed; check --deploy + makemigrations --check clean.
- **Review**: BLOCKER — `accept_invitation` (token path) created membership without role= ⇒ ADMIN silently downgraded to MEMBER (only social path updated); FIXED both paths + ADMIN-accept regression test. SHOULD-FIX — thin subtree tests → added grandchild-accept/cross-reseller-reject/cycle-terminates + direct `assert_target_in_subtree` unit tests.
- **DEVIATION (acknowledged)**: plan 4.3 named INVITATION+MEMBERSHIP; `FIELD_TO_RESOURCE_MAPPING` is one-resource-per-field by design — gated on INVITATION + DB flag, MEMBERSHIP implied. Not re-architected (out of scope). Documented in PR #88.
- **Carry-forward**: `assert_target_in_subtree(acting_org, target_org)` in public_api/capabilities.py — reuse for Phases 5 & 9 (raises GraphQLError, cycle-guarded). `OrganizationService.invite_user_to_organization(email,first_name,last_name,organization,invited_by=None,role=...)` DI-injected via `organization_service` provider (now a required dep in get_mutation_dependencies). OrgRole strawberry enum (`@strawberry.enum class OrgRole(enum.Enum)`, `.to_model_role()`) in public_api/types.py. organizations migrations now at 0010.

### Phase 4 — Self-managed invitations (email suppression + token return) ✅
- **Status**: PR #89 (https://github.com/vintasoftware/vinta-schedule-api/pull/89), base phase-3
- **Branch**: plan/whitelabel-api-provisioning/phase-4
- **Model**: claude-sonnet-4-6 (Tier 3) · implementer
- **Commits**: 2ba6070 (feat)
- **Summary**: `createInvitation` sendEmail=false branch. `invite_user_to_organization` gained `send_email: bool = True` (guards the on_commit email dispatch; existing callers default True = unchanged) and attaches the raw token as transient `invitation._raw_token` (set after save, never persisted — only token_hash/SHA-256 stored). Mutation: false → returns raw token + inviteUrl (from `HEADLESS_FRONTEND_URLS["account_accept_invitation"]`, same as email; test.py adds the test key) once; true → null (Phase 3 unchanged). Returned token validates via accept_invitation (tested end-to-end).
- **Gate**: 1929 passed; check --deploy + makemigrations --check clean.
- **Review**: no BLOCKER — all security invariants confirmed (no plaintext persistence; token validates; email truly suppressed; flag/scope/subtree gate holds on false path; true path returns null). Low-value test-convention SHOULD-FIX accepted as-is.
- **Future hardening (noted, out of scope)**: `accept_invitation` scans invitations cross-org (email__iexact + per-row verify) — latent timing/DoS surface as volume grows.

### Phase 5 — createSystemUserToken (token delegation) ✅
- **Status**: PR #90 (https://github.com/vintasoftware/vinta-schedule-api/pull/90), base phase-4
- **Branch**: plan/whitelabel-api-provisioning/phase-5 · Model: claude-sonnet-4-6 (Tier 3) · implementer
- **Commits**: 2032ba7 (feat) + e91fd16 (fix atomic + tests)
- **Summary**: `createSystemUserToken(input:{organizationId,integrationName,resources:[String!]!})` → `{systemUserId, token}` once. Gate SYSTEM_USER + assert_org_can_invite + `assert_target_in_subtree` (minted SystemUser pinned to target = acting org or descendant). Resources validated + deduped. Mint + ResourceAccess bulk_create in `transaction.atomic()`. Never touches the flag (ORGANIZATION-scoped minted tokens create only flag-false children).
- **Gate**: 1944 passed; check --deploy + makemigrations clean.
- **Review**: BLOCKER — mint+bulk_create not atomic ⇒ duplicate integration_name poisons txn / orphan SystemUser; FIXED (transaction.atomic + no-orphan regression). SHOULD-FIX — dedup test; rewrote toothless cross-tree integration test.

## PLAN AMENDED (2026-06-17, per requester)
Inserted **Phase 10a — First-party REST branding endpoints** (backend) after Phase 9. The first-party frontend edits branding over REST (project convention), distinct from Phase 6's public GraphQL `updateBranding` (reseller machine API); both write the same `OrganizationBranding` row. Phase 11b repointed to consume the REST endpoint. Phase 10a depends on Phase 6's model. Plan ordering + Touch List updated.

### Phase 6 — Branding storage + updateBranding ✅
- **Status**: PR #91 (https://github.com/vintasoftware/vinta-schedule-api/pull/91), base phase-5
- **Branch**: plan/whitelabel-api-provisioning/phase-6 · Model: claude-haiku-4-5 (Tier 2) · implementer
- **Commits**: c227a6a (feat, amended to restore deleted save()) + ac91017 (fix validation + isolation test). Also carries the plan-amendment docs commit (Phase 10a insertion).
- **Summary**: `OrganizationBranding` model (OneToOne reseller org, related_name="branding"; app_name/logo_url/primary_color/secondary_color/support_email/return_url_allowlist ArrayField) + migration 0011. `resolve_branding(org)` in organizations/models.py = getattr(get_branding_root(), "branding", None). `updateBranding` GraphQL mutation: gate BRANDING + assert_org_can_invite, upsert on acting org only, URLValidator on allowlist, hex-color + app_name validation. Added `django.contrib.postgres` to INSTALLED_APPS (ArrayField).
- **Gate**: 1958 passed; check --deploy + makemigrations clean.
- **Review**: ⚠️ CRITICAL CATCH — implementer's "outer gate" was false (ran only its 13 tests); orchestrator's full-suite run found 584 failures from an ACCIDENTAL DELETION of `return super().save()` in `OrganizationModel.save()` (broke persistence for all OrganizationModel subclasses). Restored + amended. Layer 3 audited append boundaries (clean). SHOULD-FIX: weak startswith URL check → Django URLValidator (feeds Phase 7 OAuth allowlist); app_name length/blank guards; hex regex hoisted; A-vs-B isolation regression test.
- **PROCESS NOTE**: implementer subagents have twice shipped false green gates / accidental deletions — ALWAYS re-run full `pytest -n auto` at the orchestrator (Layer 1) for remaining phases; do not trust the agent's summary count.
- **Carry-forward**: `resolve_branding(org) -> OrganizationBranding | None` (organizations/models.py) — reuse for Phase 7 (public read; MUST hide allowlist+support_email), Phase 8 (emails), Phase 10a (REST). `OrganizationBranding.objects` keyed by org; fields known. BrandingResult (GraphQL) intentionally omits support_email + return_url_allowlist. organizations migrations now at 0011.

## Current Phase
Phase 7 — brandingForTenant public read (starting)

## Remaining Phases
- Phase 7 — brandingForTenant public read
- Phase 8 — Reseller-branded emails
- Phase 9 — childOrganizations analytics
- Phase 10a — First-party REST branding endpoints (NEW, backend, depends on Phase 6)

## Deferred Phases
- Phase 10b — Frontend themed OAuth interstitials (repo: vinta-schedule-frontend-web)
- Phase 11b — Frontend reseller branding console (repo: vinta-schedule-frontend-web)
