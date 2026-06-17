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

## Current Phase
Phase 1 — createOrganization (starting)

## Remaining Phases
- Phase 2 — createUser
- Phase 3 — createInvitation (branded email)
- Phase 4 — Self-managed invitations
- Phase 5 — createSystemUserToken
- Phase 6 — Branding storage + updateBranding
- Phase 7 — brandingForTenant public read
- Phase 8 — Reseller-branded emails
- Phase 9 — childOrganizations analytics

## Deferred Phases
- Phase 10b — Frontend themed OAuth interstitials (repo: vinta-schedule-frontend-web)
- Phase 11b — Frontend reseller branding console (repo: vinta-schedule-frontend-web)
