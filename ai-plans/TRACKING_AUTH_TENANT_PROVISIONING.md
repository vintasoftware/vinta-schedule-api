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

### Phase 2 — Capture intended org name at signup ✅
- **Status**: PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 2)
- **Branch**: `plan/auth-tenant-provisioning/phase-2` (base `plan/auth-tenant-provisioning/phase-1`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/33
- **E2E**: n/a
- **Summary**: Added `Profile.pending_organization_name` (additive `users/0002` migration) + optional `organization_name` on `BaseVintaScheduleSignupForm`; `signup()` stashes the name unless a pending invite matches the email. Org is NOT created here — deferred to Phase 3's email-confirmation hook.
- **Review**: 1 should-fix → fixed. Case-sensitivity: invitation emails are un-normalized vs normalized `User.email`. Switched ALL THREE invite-match sites (`_has_pending_invitation`, `provision_tenant_for_user`, `accept_invitation`) to `email__iexact` so capture-time and provision-time matching stay consistent (asymmetric matching would orphan a user). Added case-insensitivity regression tests. Gate: 1180 passed.
- **Cross-phase note**: this phase modified Phase 1 code (`organizations/services.py` match filters) — intentional, for invite-match consistency.

### Phase 3 — Create own org on email verification (no invite) ✅
- **Status**: PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 3)
- **Branch**: `plan/auth-tenant-provisioning/phase-3` (base `plan/auth-tenant-provisioning/phase-2`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/34
- **E2E**: n/a
- **Summary**: `accounts/signals.py` `email_confirmed` handler → DI-resolve `OrganizationService` → `provision_tenant_for_user(user, organization_name=profile.pending_organization_name)`; clears the name on success, swallows `UserAlreadyHasMembershipError` (idempotent). Wired via `AccountsConfig.ready()` with `dispatch_uid`.
- **Open Question 1 RESOLVED**: `email_confirmed` DOES fire for code-based headless verification (verified in allauth 65.18.0 source — flow funnels through `verify_email()`). Signal handler chosen over adapter override.
- **Review**: no blockers; 1 should-fix (re-confirmation test now drives the real `verify_email` path) + 2 nits fixed. Gate: 1185 passed.
- **Note for Phase 4**: the handler already covers the invited (MEMBER) branch via invite-first service; Phase 4 adds targeted UC2 tests.

## Current Phase
- Phase 4 — Auto-join invited org on email verification (next) — UC2

## Remaining Phases
- Phase 4 — Auto-join invited org on email verification (Tier 2) — UC2
- Phase 5 — Gated onboarding for uninvited social signup (Tier 2) — UC3
- Phase 6 — Auto-join invited org on social signup (Tier 3) — UC4
- Phase 7 — Reject already-member invite acceptance at the API (Tier 2) — UC5
- Phase 8 — Audit + close the hard gate (Tier 3)

## Deferred Phases
- none (no cross-repo, no flag-removal)
