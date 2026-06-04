# Tracking — AUTH_TENANT_PROVISIONING

- **Feature**: Auth–Tenant Provisioning (wire signup → organization/tenant)
- **Plan**: [ai-plans/2026-06-04-AUTH_TENANT_PROVISIONING_IMPLEMENTATION_PLAN.md](2026-06-04-AUTH_TENANT_PROVISIONING_IMPLEMENTATION_PLAN.md)
- **Spec**: [ai-plans/2026-06-04-AUTH_TENANT_PROVISIONING_SPEC.md](2026-06-04-AUTH_TENANT_PROVISIONING_SPEC.md)
- **Started**: 2026-06-04
- **Last updated**: 2026-06-04
- **Feature flag**: none (shipped unflagged — known-requirement fix)
- **Run options**: auto-flow (no per-phase pause), inline PR comments ON
- **Branch pattern**: `plan/auth-tenant-provisioning/phase-{id}`, stacked

## Completed Phases

### Phase 1 — Central tenant-provisioning service ✅
- **Status**: merged-ready (PR open)
- **Model**: claude-sonnet-4-6 (plan tier: Tier 3)
- **Branch**: `plan/auth-tenant-provisioning/phase-1` (base `main`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/32
- **E2E**: n/a (backend-only)
- **Summary**: Added `UserAlreadyHasMembershipError` (DRF `ValidationError` → 400) and `OrganizationService.provision_tenant_for_user(user, organization_name=None)` — the single guarded entry point: already-member → raise; non-expired/unaccepted invitation matching `user.email` → MEMBER join + mark invitation accepted; else `organization_name` → `create_organization` (ADMIN); else `None` (gated onboarding). Hardened `accept_invitation` to raise the typed error before the DB write and wrap the insert in a savepoint (avoids `ATOMIC_REQUESTS` transaction-poison). Fixed the `IntegrityError` import to Django's class so the concurrency backstop actually catches `.create()` violations.
- **Review**: 3 BLOCKERs found + fixed (wrong `IntegrityError` class; concurrency test never exercised the backstop; missing savepoint in `accept_invitation`) + 1 should-fix (unguarded `create_organization` delegation) + 1 nit. Final gate: `check --deploy` clean, `pytest -n auto` = 1170 passed.
- **Key API for later phases**: `OrganizationService.provision_tenant_for_user(user, organization_name=None)` — invite-first; returns the membership or `None`. Phases 3/4/6 call this; Phase 7 surfaces `UserAlreadyHasMembershipError` at the accept endpoint.

## Current Phase
- Phase 2 — Capture intended org name at signup (next)

## Remaining Phases
- Phase 2 — Capture intended org name at signup (Tier 2)
- Phase 3 — Create own org on email verification, no invite (Tier 3) — UC1
- Phase 4 — Auto-join invited org on email verification (Tier 2) — UC2
- Phase 5 — Gated onboarding for uninvited social signup (Tier 2) — UC3
- Phase 6 — Auto-join invited org on social signup (Tier 3) — UC4
- Phase 7 — Reject already-member invite acceptance at the API (Tier 2) — UC5
- Phase 8 — Audit + close the hard gate (Tier 3)

## Deferred Phases
- none (no cross-repo, no flag-removal)
