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

### Phase 4 — Auto-join invited org on email verification ✅
- **Status**: PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 2)
- **Branch**: `plan/auth-tenant-provisioning/phase-4` (base `plan/auth-tenant-provisioning/phase-3`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/35
- **E2E**: n/a
- **Summary**: Tests-only (no production code). New `accounts/tests/test_email_invite_autojoin.py` — 4 integration tests driving the real signup form + real `verify_email`: full end-to-end invited auto-join (MEMBER, invitation accepted+linked, no stray org); invite-wins-over-name; case-insensitive invite; accepted-marker. Confirms Phases 1–3 compose into Use-case 2.
- **Review**: Layer 3 done as direct diff audit (tests-only, production paths already reviewed in P1–P3). Gate: 1189 passed.

### Phase 5 — Gated onboarding for uninvited social signup ✅
- **Status**: PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 2)
- **Branch**: `plan/auth-tenant-provisioning/phase-5` (base `plan/auth-tenant-provisioning/phase-4`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/36
- **E2E**: n/a
- **Summary**: Tests-only. `SocialAccountAdapter.save_user` confirmed to leave uninvited social users membership-less (no org/membership). New `accounts/tests/test_social_gated_onboarding.py` (6 tests): save_user membership-less; gated→create→ADMIN; second-create 403; membership-less blocked from invitation list/create. Existing `OrganizationManagementPermission`/`OrganizationInvitationPermission` provide the gate.
- **Review**: Layer 3 as direct diff audit (tests-only; gate is existing reviewed permission code). Gate: 1195 passed.

### Phase 6 — Auto-join invited org on social signup ✅
- **Status**: PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 3)
- **Branch**: `plan/auth-tenant-provisioning/phase-6` (base `plan/auth-tenant-provisioning/phase-5`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/37
- **E2E**: n/a
- **Summary**: `SocialAccountAdapter._provision_org_membership(user)` called at end of `save_user` → `provision_tenant_for_user(user)` (no name): invited→MEMBER join, uninvited→stays gated (Phase 5 preserved), already-member→swallowed. DI via container (signals.py pattern). Blank-email guard added.
- **Review**: no blockers; SHOULD-FIX (blank-email guard) + savepoint-hygiene gap in `provision_tenant_for_user` (both `create()` sites now wrapped in inner `with transaction.atomic()`, matching Phase 1's `accept_invitation` fix — closes the same poisoning bug on the service both the email hook and social adapter call) + test NIT (added genuine already-member-guard load test + 2 transaction-not-poisoned regression tests). Gate: 1202 passed.

### Phase 7 — Reject already-member invite acceptance at the API ✅
- **Status**: PR open
- **Model**: claude-sonnet-4-6 (plan tier: Tier 2)
- **Branch**: `plan/auth-tenant-provisioning/phase-7` (base `plan/auth-tenant-provisioning/phase-6`)
- **PR**: https://github.com/vintasoftware/vinta-schedule-api/pull/38
- **E2E**: n/a
- **Summary**: `AcceptInvitationView.create()` now explicitly catches `UserAlreadyHasMembershipError` → 400 `{"error": "User already belongs to an organization."}`. Tests: already-member + valid token to another org → 400, original membership unchanged, target invitation stays pending, no second membership.
- **Review**: Layer 2 found body-shape inconsistency (`{code,detail}` vs siblings' `{error}`) → fixed to match sibling handlers. Gate: 1204 passed.

## Current Phase
- Phase 8 — Audit + close the hard gate (next)

## Remaining Phases
- Phase 8 — Audit + close the hard gate (Tier 3)

## Deferred Phases
- none (no cross-repo, no flag-removal)
