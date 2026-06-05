# Tracking — REST API Frontend Gaps

- **Plan**: ai-plans/2026-06-05-REST_API_FRONTEND_GAPS_IMPLEMENTATION_PLAN.md
- **Plan id**: rest-api-frontend-gaps
- **Started**: 2026-06-05
- **Last updated**: 2026-06-05
- **Feature flag**: none (no flag system in project; additive surface)
- **Run options**: pause_between_phases=false (auto-flow); generate_inline_comments=true
- **Branch pattern**: `plan/rest-api-frontend-gaps/phase-{id}` (stacked; phase-0 off `main`)

## Completed Phases

### Phase 0 — Reusable IsOrganizationAdmin permission ✅
- **Status**: done, reviewed (3 layers clean), pushed, PR opened.
- **Model**: claude-haiku-4-5 (plan tier 2).
- **Branch**: plan/rest-api-frontend-gaps/phase-0 (base: main)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/42
- **PR-context**: .vinta-ai-workflows/prs-context/rest-api-frontend-gaps/phase-0.md (published)
- **Files**: organizations/permissions.py, organizations/tests/test_permissions.py
- **Summary**: Added `IsOrganizationAdmin(BasePermission)`. `has_permission` = authenticated + membership present (safe getattr; hard-gate preserved). `has_object_permission` = object org matches membership org AND `User.is_organization_admin(org_id)` (the method already existed at users/models.py:46 — no adaptation needed). Handles Organization + OrganizationModel subclasses, mirroring OrganizationManagementPermission. 11 unit tests. Outer gate green (1243 passed). Deliberately no `membership.is_active` reference — that lands in Phase 1.
- **Deviations**: none.

### Phase 1 — Add OrganizationMembership.is_active ✅
- **Status**: done, reviewed (3 layers; Layer 3 caught 2 security BLOCKERs + leaks → fixer commit + regression tests), pushed, PR opened.
- **Model**: claude-sonnet-4-6 (plan tier 1; bumped for the multi-site hard-gate audit). Fixer: sonnet.
- **Branch**: plan/rest-api-frontend-gaps/phase-1 (base: phase-0)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/43
- **PR-context**: .vinta-ai-workflows/prs-context/rest-api-frontend-gaps/phase-1.md (published)
- **Commits**: feat (field + helper + 19 read/permission gate sites); fix (write-path serializer gates + OrganizationManagementPermission object access + public_api user leak + tests).
- **Key**: `is_active` field (default+db_default True, indexed). New `get_active_organization_membership(user)` helper = single source of truth (None for missing OR inactive). Gate closed across get_queryset, all relevant permissions, serializer create/save WRITE paths, OrganizationViewSet.current, public GraphQL users query, User.is_organization_admin. Design note: inactive membership blocks re-provisioning; reactivation (Phase 3) is the un-disable path. Outer gate green (1262 passed); mypy adds no new errors (8 pre-existing in serializers.py).
- **Deviations**: extended gating to OrganizationInvitationPermission (consistency).

### Phase 2 — List organization members (admin) ✅
- **Status**: done, reviewed (3 layers; Layer 3 confirmed the IsOrganizationAdmin collection-level flaw the implementer flagged → fixer), pushed, PR opened.
- **Model**: claude-haiku-4-5 (tier 2); fixer: haiku.
- **Branch**: plan/rest-api-frontend-gaps/phase-2 (base: phase-1)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/44
- **PR-context**: .vinta-ai-workflows/prs-context/rest-api-frontend-gaps/phase-2.md (published)
- **Key**: `OrganizationMembershipViewSet` (ReadOnly, IsOrganizationAdmin), `OrganizationMembershipSerializer` (read-only, flattened user email + profile name, select_related no-N+1, no PII leak), route `organization-members`, schema regen. List returns active+inactive. IMPORTANT FIX: `IsOrganizationAdmin.has_permission` now requires `membership.is_admin` (was membership-only) — gates collection actions; reusable by all future admin endpoints with no per-view override. Outer gate green (1272).
- **Deviations**: none (the permission fix is reused infra, benefits later phases).

### Phase 3 — Deactivate/reactivate a member (admin) ✅
- **Status**: done, reviewed (3 layers; Layer 3 surfaced that the last-admin 400 is unreachable → documented + sole-admin self-deactivate test), pushed, PR opened.
- **Model**: haiku (tier 2); fixer: haiku.
- **Branch**: plan/rest-api-frontend-gaps/phase-3 (base: phase-2) — **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/45
- **PR-context**: phase-3.md (published)
- **Key**: `deactivate`/`reactivate` detail @actions on OrganizationMembershipViewSet; self-lockout guard (403) enforces the org-keeps-an-admin invariant; last-admin guard kept as documented defense-in-depth (unreachable via HTTP). Idempotent. Outer gate green (1285). mypy baseline = 108 full-project errors (pre-existing, test files); confirmed zero new across phases.
- **Deviations**: none.

### Phase 4 — Request own calendar import ✅
- **Status**: done, reviewed (3 layers; Layer 3 → import ALL connected accounts, fixed with closure-safe fresh-service-per-account), pushed, PR opened.
- **Model**: haiku (tier 2); fixers: haiku x1.
- **Branch**: phase-4 (base: phase-3) — **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/46
- **Key**: `POST /calendar/request-import/` (@action detail=False). Imports all caller SocialAccounts; per-account fresh `container.calendar_service()` + default-arg closure binding to avoid on_commit shared-state bug; `transaction.on_commit` defers `task.delay`. 202+detail. 400 no-account, 403 membership-less. Outer gate green (1290), mypy 108.
- **Known nit**: unused injected `calendar_service` param remains (noted in PR; cleanup later).
- **Pattern for Phases 5/6/7**: resolve fresh service per target if deferring authenticate()+enqueue; otherwise authenticate inline + call synchronously is fine when returning a result.

### Phase 5 — Request own calendar sync ✅
- **Status**: done, reviewed (3 layers; Layer 3 → 2 fixes: use serializer for input validation + guard None social account), pushed, PR opened.
- **Model**: haiku; fixers: haiku x1. **Branch**: phase-5 (base phase-4). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/47
- **Key**: `POST /calendar/{id}/request-sync/` owner-only (get_object org-scope + CalendarOwnership check). Input via `CalendarSyncRequestSerializer`. None-account → 400. Returns `CalendarSync` (id+status) at 202. New `CalendarSyncSerializer`. Outer gate green (1297), mypy 108.
- **Out-of-scope flagged**: `request_calendar_sync` enqueues `.delay` without on_commit (pre-existing; phase 6 shares it). Candidate follow-up.

### Phase 6 — Admin syncs another user's calendar ✅
- **Status**: done, reviewed (3 layers, clean), pushed, PR opened.
- **Model**: haiku. **Branch**: phase-6 (base phase-5). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/48
- **Key**: `POST /calendar/{id}/admin-sync/` with `permission_classes=[IsOrganizationAdmin]` (admin-only, cross-org 404). Authenticates with the calendar OWNER's SocialAccount (resolved via CalendarOwnership default-first), NOT the admin's — test proves it. Guards: no owner / no linked account / bad datetimes → 400. Outer gate green (1304), mypy 108.

### Phase 7 — Trigger org rooms/resources sync (admin) ✅
- **Status**: done, reviewed (3 layers; Layer 3 #1 → transition was on an unreachable update endpoint + tests patched perms → opened admin update via get_permissions; #2 → hardened: dropped dead on_commit try/except, fixed schema, select_for_update against double-fire), pushed, PR opened.
- **Model**: sonnet (resumed after a socket error mid-run); fixers: sonnet + haiku. **Branch**: phase-7 (base phase-6). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/49
- **Key**: `OrganizationService.request_rooms_sync` (DRY, used by create_organization too); `POST /organizations/{id}/sync-rooms/` admin action; should_sync_rooms False→True transition fires once (locked). **IMPORTANT reusable infra**: `OrganizationViewSet.get_permissions()` now gates update/partial_update with IsOrganizationAdmin — admins can configure their own org (previously update was unreachable for all). Outer gate green (1320), mypy 108.
- **Reusable note**: org update is admin-only now; org-config edits go through PATCH /organizations/{id}/.

### Phase 8 — Transfer event between calendars (admin) ✅
- **Status**: done, reviewed (3 layers; Layer 3 → added same-calendar no-op test), pushed, PR opened.
- **Model**: sonnet; fixer: haiku. **Branch**: phase-8 (base phase-7). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/50
- **Key**: `POST /calendar-events/{id}/transfer/` admin-only; authenticates with SOURCE calendar owner's account (transfer reads source event from provider); target resolved via filter_by_organization; same-calendar no-op → 400. Outer gate green (1329), mypy 108.

### Phase 9 — Calendar soft-disable ✅
- **Status**: done, reviewed (3 layers; Layer 3 → documented/tested include_inactive opt-in on action routes + dropped internal phrasing), pushed, PR opened.
- **Model**: sonnet; fixer: haiku. **Branch**: phase-9 (base phase-8). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/51
- **Key**: `Calendar.is_active` (migration 0011, db_default True). get_queryset hides inactive unless `?include_inactive=true` (uniform across actions). DELETE → soft-disable (204, idempotent). is_active read-only in serializer. Outer gate green (1338), mypy 108.
- **FOLLOW-UP for Phase 10/11**: serializer querysets (bundle-create, resource-allocation) + public GraphQL calendars query don't filter is_active → disabled calendars still selectable/listed there. Consider when touching bundles.

### Phase 10 — Update calendar bundle ✅
- **Status**: done, reviewed (3 layers; Layer 3 → fixed disabled-existing-child trap via union queryset), pushed, PR opened.
- **Model**: sonnet; fixer: haiku. **Branch**: phase-10 (base phase-9). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/52
- **Key**: `PATCH /calendar/{id}/bundle/` admin-only; `CalendarService.update_bundle_calendar` atomic reconciliation (add/remove children, exactly-one primary, validates bundle/primary/cross-org/no-nesting); serializer child queryset = active OR existing-children (keeps disabled existing, bars new disabled). Outer gate green (1349), mypy 108.

### Phase 11 — Disable calendar bundle ✅
- **Status**: done, reviewed (3 layers, clean), pushed, PR opened.
- **Model**: sonnet. **Branch**: phase-11 (base phase-10). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/53
- **Key**: destroy gating by type — BUNDLE → admin-only; non-bundle → owner-or-admin (tightened Phase 9's ungated destroy). Bundle disable leaves events/representations/children intact (Open Q #3). Outer gate green (1359), mypy 108.

### Phase 12 — Create public-API token (admin) ✅
- **Status**: done, reviewed (3 layers; Layer 3 → BLOCKER: savepoint for duplicate-name under ATOMIC_REQUESTS + schema/dead-code fixes), pushed, PR opened.
- **Model**: sonnet; fixer: haiku. **Branch**: phase-12 (base phase-11). **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/54
- **Key**: NEW public_api REST surface — `SystemUserTokenViewSet` (create-only), `public_api/routes.py` wired into urls. `POST /public-api-tokens/` admin-only → create_system_user, persist ResourceAccess, return plaintext token ONCE (hash never serialized). Duplicate name → 400 via nested savepoint (ATOMIC_REQUESTS-safe). Serializers: `SystemUserTokenCreateSerializer`, `SystemUserTokenResponseSerializer`. Outer gate green (1376), mypy 108.
- **Infra for 13/14/15**: extend SystemUserTokenViewSet; org-scoped get_queryset already returns SystemUser for caller's org (anon-guarded).

## Current Phase
Phase 13 — List public-API tokens (admin) (next). Add list+retrieve to SystemUserTokenViewSet; new list serializer exposing id/integration_name/is_active/available_resources — NEVER token or long_lived_token_hash. Tier 2.

## Remaining Phases
14 (token revoke), 15 (token edit perms), 16 (events expanded).

## Reusable infra notes (for later phases)
- `IsOrganizationAdmin` (organizations/permissions.py) — admin gate, works at collection + object level. Use for all admin endpoints.
- `get_active_organization_membership(user)` (organizations/models.py) — canonical active-membership resolver (None for missing OR inactive).
- DI in actions: `@inject` + `Annotated[Service, Provide["service_name"]]`; authenticate CalendarService with the user's SocialAccount (see CalendarViewSet.available_windows).

## Deferred Phases
None (no cross-repo, no flag-removal phases in this plan).
