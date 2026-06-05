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

## Current Phase
Phase 2 — List organization members (admin) (next).

## Remaining Phases
3 (deactivate/reactivate member), 4 (request own import), 5 (request own sync), 6 (admin sync other), 7 (rooms sync trigger), 8 (transfer event), 9 (calendar soft-disable), 10 (bundle update), 11 (bundle disable), 12 (token create), 13 (token list), 14 (token revoke), 15 (token edit perms), 16 (events expanded).

## Deferred Phases
None (no cross-repo, no flag-removal phases in this plan).
