# Auth–Tenant Provisioning — Implementation Plan

Spec: [2026-06-04-AUTH_TENANT_PROVISIONING_SPEC.md](2026-06-04-AUTH_TENANT_PROVISIONING_SPEC.md). This plan translates that spec into phased delivery; it does not re-derive requirements.

## 1. Goals

1. Every newly created user exits onboarding bound to exactly one organization — by creating their own (becoming ADMIN) or by auto-joining the organization that invited them (becoming MEMBER) — across both email/password and social signup.
2. Centralize the user→tenant provisioning decision (already-member guard, invite-auto-join, create-from-name) in one service so every signup path calls the same guarded code.
3. Preserve the one-organization-per-user invariant: no path creates a second membership; conflicts surface a clear error, never a database integrity error or a silent org switch.
4. Enforce the hard gate: an authenticated user with no membership cannot reach tenant-scoped endpoints; only onboarding (create-org / accept-invite) responds.

**Non-goals:**
- Multiple organizations per user (the `OneToOneField` on `OrganizationMembership` stays).
- Switching or leaving organizations.
- Promote/demote of members (ADMIN remains creator-only).
- Redesigning the invitation mechanism (token, 7-day expiry, email addressing, send/revoke unchanged).
- Introducing a feature-flag system (shipped unflagged — see **Guiding Decisions**).
- Any Playwright/E2E surface — this repository is API-only; no browser is reached, so no `QA_USE_CASES.md` or e2e specs are created.
- Onboarding UI/screens (frontend is a separate concern/repo).

## 2. Guiding Decisions

> **Amended 2026-06-04**: changed the **email-confirmation provisioning mechanism** from an `email_confirmed` signal handler to an imperative `AccountAdapter.confirm_email` override (see the "Email-confirmation provisioning mechanism" row). Affects Phase 3 (rewrite) and Phase 4 (test helper).

| Decision | Resolution |
|---|---|
| **No feature flag** | Shipped unflagged. The spec frames this as a *known requirement* fixing a broken state (orphaned users), not a hypothesis to A/B. The repo has no flag system today; introducing one to gate a correctness fix is unjustified overhead. Rollout safety comes from per-phase tests (each phase is independently mergeable and the pre-feature path stays valid until its phase lands). |
| **Org created after email verification (email/password path)** | `ACCOUNT_EMAIL_VERIFICATION = "mandatory"`. Provisioning is deferred to the email-confirmation moment so we never create organizations for never-verified signups. Requires stashing the intended organization name at signup-form time and consuming it on confirmation. |
| **Email-confirmation provisioning mechanism** | **Imperative `AccountAdapter.confirm_email` override**, not an `email_confirmed` signal handler. The override sits directly in the confirmation call path (`mark_email_address_as_verified → adapter.confirm_email`), giving a discoverable stack trace and step-through debugging, and makes the email path **symmetric with the social path** (both adapters provision via an explicit method call delegating to `provision_tenant_for_user`). Per-adapter methods, not a shared helper: the email adapter passes the stashed `pending_organization_name`; the social adapter passes none. Note: `confirm_email` internally calls `verify_email` (which emits the now-unused signal), so tests must drive confirmation via `confirm_email`. |
| **Pending org name storage** | New nullable `pending_organization_name` on `Profile` (already created/touched in `BaseVintaScheduleSignupForm.signup()`). Cleared once provisioning succeeds. Null/blank for invited signups (they auto-join, no name needed). |
| **Social path provisions at signup/first-login** | `SOCIALACCOUNT_EMAIL_VERIFICATION = None` — social has no verification step. Invited social users auto-join inside the social adapter; non-invited social users land membership-less (gated) and create their org via the existing `OrganizationViewSet` create endpoint. |
| **Single provisioning service** | One guarded entry point (`OrganizationService.provision_tenant_for_user(user, organization_name=None)`) used by the email-confirmation hook, the social adapter, and the explicit accept-invite endpoint. Invite-first: if a non-expired invitation matches `user.email`, auto-join; else if a name is supplied, create an org; the already-member case raises a typed error. |
| **Invite detection is by email, exactly one invite** | `OrganizationInvitation.email` is `unique=True` globally, so at most one pending invitation exists per email. The spec's "multiple invitations for the same email" open question is therefore moot at the DB level — no tie-break logic needed. |
| **Org fields at signup** | Name only; `should_sync_rooms` stays `False` at signup. Matches the spec's recommended default. |
| **Hard gate via audit, not new framework** | Keep the existing per-viewset `user.organization_membership` checks; add a phase that audits every tenant-scoped entry point for unguarded membership access and patches gaps. No new global permission framework. |
| **Already-member error is typed + surfaced** | A dedicated exception (e.g. `UserAlreadyHasMembershipError`) maps to a 4xx with a clear message at the accept-invite endpoint and is swallowed-as-no-op where auto-join races a manual create. |

## 3. Data Model Changes

### 3.1 `Profile.pending_organization_name`

Add to the `Profile` model in @users/models.py:

```python
pending_organization_name = models.CharField(max_length=255, blank=True, default="")
```

- Nullable-by-blank, default empty. Holds the organization name captured at email/password signup until `email_confirmed` provisioning consumes it, then reset to `""`.
- Migration via the `add-migration` skill (single additive column, no lock concern — `Profile` is not a hot/partitioned table).

### 3.2 Type plumbing

- New exception `UserAlreadyHasMembershipError` in @organizations/exceptions.py, alongside the existing `DuplicateInvitationError` / `InvalidInvitationTokenError` / `InvitationNotFoundError`.
- No TypedDict/dataclass changes; provisioning takes plain `user` + optional `organization_name`.

## 4. API Design

No new endpoints. Contract changes only:

### 4.1 `POST /auth/...` (allauth headless signup)
- Email/password signup gains an optional `organization_name` form input, consumed by `BaseVintaScheduleSignupForm.signup()` and stashed on `Profile.pending_organization_name`. Ignored when a pending invitation matches the email. No response-shape change (allauth-owned response).

### 4.2 `POST /invitations/accept` (`AcceptInvitationView`)
- When the authenticated user already holds a membership, returns a 4xx with a clear error body ("user already belongs to an organization") instead of raising an uncaught integrity error. Success contract unchanged.

### 4.3 `POST /organizations/` (`OrganizationViewSet` create)
- Unchanged. Remains the create-org path used by gated (membership-less) social users during onboarding. `OrganizationManagementPermission` already permits create only when the user has no membership.

## 5. Phased Rollout

Foundation first (shared provisioning + storage), then one phase per spec use-case, then the gate audit. No cross-repo dependency, so ordering is foundation → use-cases → hardening.

### Phase 1 — Central tenant-provisioning service

**Goal**: one guarded service method that turns a membership-less user into a member — invite-auto-join, create-from-name, or typed rejection. Ship value: none user-visible on its own (scaffolding the other phases consume).

**Feature flag**: none — shipping unflagged per **Guiding Decisions**; this is new internal surface no existing caller reaches yet.

Changes:
1. @organizations/exceptions.py: add `UserAlreadyHasMembershipError`.
2. @organizations/services.py: add `provision_tenant_for_user(self, user, organization_name=None)`. Logic, in order: if `user` already has `organization_membership` → raise `UserAlreadyHasMembershipError`; else find a non-expired, unaccepted `OrganizationInvitation` matching `user.email` → create the membership (MEMBER), mark invitation accepted + linked; else if `organization_name` truthy → delegate to existing `create_organization(creator=user, name=organization_name)`; else return `None` (caller decides — gated onboarding).
3. @organizations/services.py: harden `accept_invitation` to guard the `OneToOne` — if the user already has a membership, raise `UserAlreadyHasMembershipError` rather than letting `OrganizationMembership.objects.create` hit the integrity error.
4. Wrap membership creation so a concurrency loser surfaces `UserAlreadyHasMembershipError`, not a raw DB error.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @organizations/tests/test_services.py — `provision_tenant_for_user` across all three branches (invite present, name-only, already-member raises); `accept_invitation` now raises `UserAlreadyHasMembershipError` for an already-member user; expired invitation is ignored (falls through to name/None).
- **Integration**: concurrency guard — two provisioning attempts for the same user yield exactly one membership and one `UserAlreadyHasMembershipError`.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Multi-branch business logic with a concurrency/idempotency guard touching the tenant root.

**Reusable skills**: none.

Acceptance: `provision_tenant_for_user` and the hardened `accept_invitation` behave per the three branches with full unit coverage; no path can create a second membership.

### Phase 2 — Capture intended org name at signup

**Goal**: email/password signup persists the organization name (or nothing, if invited) for later provisioning. Ship value: stored intent; no org created yet.

**Feature flag**: none.

Changes:
1. @users/models.py: add `Profile.pending_organization_name` (see **Data Model Changes**); migration via `add-migration`.
2. @accounts/base_forms.py: `BaseVintaScheduleSignupForm` gains an optional `organization_name` field; `signup()` stashes it on the profile **only when no pending invitation matches the signup email** (invited signups leave it blank).

Spec use-case: shared scaffolding for the email/password use-cases.

Tests:
- **Unit**: @accounts/tests/ — form with `organization_name` and no matching invite stores it on the profile; form with a matching pending invitation leaves `pending_organization_name` blank.
- **Integration**: signup POST persists the field; existing signup behavior (user + profile + names) unchanged.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single additive field + form wiring against existing precedent.

**Reusable skills**: `add-migration`.

Acceptance: a verified-or-not user record after email signup carries the intended org name (uninvited) or blank (invited); no organization created yet.

### Phase 3 — Create own org on email verification (no invite)

> **Amended 2026-06-04**: reworked from an `email_confirmed` **signal handler** to an imperative **`AccountAdapter.confirm_email` override** (Option A). Rationale: the signal was event-driven and hard to step-debug; the override puts provisioning directly in the confirmation call path (discoverable stack trace, single call site) and makes the email path **symmetric with the social path** (`SocialAccountAdapter.save_user`, Phase 6 — both adapters provision via an explicit method call). See **Guiding Decisions**.

**Goal**: an uninvited email/password user becomes ADMIN of a new org the moment their email is confirmed.

**Feature flag**: none.

Changes:
1. @accounts/account_adapters.py: override `AccountAdapter.confirm_email(self, request, email_address)`. Call `confirmed = super().confirm_email(request, email_address)`; when `confirmed`, resolve `OrganizationService` (same DI-container pattern the `SocialAccountAdapter` uses) and call `provision_tenant_for_user(user, organization_name=profile.pending_organization_name)` where `user = email_address.user`; on success clear `pending_organization_name`. Return `confirmed`. Guard a missing profile. **Per-adapter methods** (not a shared helper): `AccountAdapter` passes the stashed org name; `SocialAccountAdapter` passes none — keep the two small and symmetric.
2. Idempotent: catch `UserAlreadyHasMembershipError` and swallow (re-confirmation → no-op).
3. Remove the signal-based wiring introduced by the original Phase 3: delete `accounts/signals.py` (the `email_confirmed` handler) and its `AccountsConfig.ready()` connection. The production confirmation flow reaches the override via `mark_email_address_as_verified → adapter.confirm_email`.

> **Note on the confirmation API (verified in allauth 65.18.0)**: `AccountAdapter.confirm_email` internally calls `email_verification.verify_email`, which is what sends the (now-unused) `email_confirmed` signal. Tests must therefore drive provisioning through `adapter.confirm_email(request, email_address)` — calling `verify_email` directly bypasses the override. This is why Phase 4's test helper also changes (see Phase 4).

Spec use-case: Use-case 1 (self-service email/password signup, no invite).

Tests:
- **Integration**: @accounts/tests/ — uninvited user signs up with `organization_name`, confirms email **through `AccountAdapter.confirm_email`** → org + ADMIN membership exist, `pending_organization_name` cleared; re-confirmation is a no-op (no second org).
- **Integration**: confirming with blank `pending_organization_name` and no invite → no org, user remains gated.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Adapter-lifecycle override + idempotent provisioning, with care that the DI resolution + return-value contract match allauth's `confirm_email`.

**Reusable skills**: none.

Acceptance: confirming an uninvited email signup (via `AccountAdapter.confirm_email`) yields exactly one organization with the user as ADMIN; double-confirm creates nothing extra; no `email_confirmed` signal handler remains in the codebase.

### Phase 4 — Auto-join invited org on email verification

> **Amended 2026-06-04**: test-only follow-on of the Phase 3 rework. The integration tests drive confirmation through `AccountAdapter.confirm_email` (not `verify_email` directly), so they exercise the new override. No production change in this phase.

**Goal**: an invited email/password user joins the inviting org (MEMBER) on email confirmation, with no separate accept step and no stray org.

**Feature flag**: none.

Changes:
1. No new branch needed in the service (Phase 1 is invite-first); this phase wires + proves the email path end-to-end: a signup whose email matches a pending invitation, on confirmation **through `AccountAdapter.confirm_email`**, auto-joins via `provision_tenant_for_user` and marks the invitation accepted.
2. Confirm the Phase 2 capture-skip holds (no `pending_organization_name` for invited signups), so no org is created even if a name were somehow present — invite still wins.
3. Update the test helper that confirms the email so it calls `AccountAdapter.confirm_email(request, email_address)` (the amended Phase 3 hook), not `verify_email` directly.

Spec use-case: Use-case 2 (email/password signup by an invited address).

Tests:
- **Integration**: @accounts/tests/ — pending invitation + matching email signup, confirm email → MEMBER membership in the inviting org, invitation `accepted_at`/`membership` set, zero new organizations.
- **Integration**: invited signup that also submitted an `organization_name` → invite wins, no stray org.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Mostly integration coverage over Phase 1's existing branch.

**Reusable skills**: none.

Acceptance: an invited email signup lands in the inviting org as MEMBER on confirmation, with no new organization and no explicit accept call.

### Phase 5 — Gated onboarding for uninvited social signup

**Goal**: an uninvited social user lands authenticated-but-membership-less and can create their org through the existing create endpoint, becoming ADMIN.

**Feature flag**: none.

Changes:
1. @accounts/account_adapters.py: confirm `SocialAccountAdapter.save_user()` leaves the user membership-less for the uninvited case (no silent org creation), preserving today's behavior intentionally.
2. Verify `OrganizationViewSet` create + `OrganizationManagementPermission` already serve the gated social user (create allowed only when no membership) — no code change expected, covered by tests.

Spec use-case: Use-case 3 (social signup, no invite).

Tests:
- **Integration**: @accounts/tests/ + @organizations/tests/ — uninvited social signup → no membership; the user can POST `/organizations/` and becomes ADMIN; a second create attempt (now a member) is rejected by `OrganizationManagementPermission`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Largely assertion of existing behavior plus a guard test.

**Reusable skills**: none.

Acceptance: an uninvited social user is gated until they create an org via the existing endpoint, after which they are ADMIN and cannot create a second.

### Phase 6 — Auto-join invited org on social signup

**Goal**: an invited social user joins the inviting org (MEMBER) automatically at social signup, skipping the create-org prompt entirely.

**Feature flag**: none.

Changes:
1. @accounts/account_adapters.py: in `SocialAccountAdapter.save_user()` (after `super().save_user()` persists the user/profile), call `provision_tenant_for_user(user)` so a pending invitation matching the social email auto-joins. No invitation → returns `None`, user stays gated (Phase 5 behavior).
2. Guard against the already-member case (re-login of a social user who is already a member → no-op).

Spec use-case: Use-case 4 (social signup by an invited address).

Tests:
- **Integration**: @accounts/tests/ — pending invitation + social signup with matching email → MEMBER membership in the inviting org, no create-org prompt path taken, invitation marked accepted; re-login is a no-op.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Social adapter lifecycle + provisioning interaction with the headless serialization round-trip.

**Reusable skills**: none.

Acceptance: an invited social signup auto-joins the inviting org as MEMBER without any create-org step.

### Phase 7 — Reject already-member invite acceptance at the API

**Goal**: an already-member user attempting to accept/redeem an invite to another org gets a clear 4xx, with their existing membership untouched.

**Feature flag**: none.

Changes:
1. @organizations/views.py + @organizations/serializers.py: `AcceptInvitationView` / `AcceptInvitationSerializer` catch `UserAlreadyHasMembershipError` (from Phase 1's hardened `accept_invitation`) and return a clear error response.

Spec use-case: Use-case 5 (invited address that already has an account and a membership).

Tests:
- **Integration**: @organizations/tests/test_views.py — an authenticated member POSTs `/invitations/accept` with a valid token for another org → 4xx with the documented message; their original membership/org/role unchanged; the invitation remains pending.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Exception-to-response mapping with precedent in the existing view.

**Reusable skills**: none.

Acceptance: accepting an invite while already a member returns a clear error and changes no state.

### Phase 8 — Audit + close the hard gate

**Goal**: guarantee no tenant-scoped endpoint serves a membership-less user; patch any unguarded `user.organization_membership` access.

**Feature flag**: none.

Changes:
1. Audit every tenant-scoped viewset/serializer that reads `user.organization_membership` (calendar_integration, payments, webhooks, public_api, organizations, etc.) for paths that assume a membership exists. `grep -r "organization_membership"`.
2. Patch gaps so a membership-less user is refused (empty queryset / permission denial) rather than erroring or leaking.
3. Document the invariant ("authenticated implies gated-onboarding or has-membership") near the membership model.

Spec use-case: cross-cutting hardening for the spec's hard-gate objective.

Tests:
- **Integration**: per patched surface — a membership-less authenticated user hits tenant-scoped endpoints and is uniformly refused; onboarding endpoints (create-org, accept-invite) remain reachable.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Cross-app audit with judgment about each call site.

**Reusable skills**: `systematic-debugging` (for tracing membership-less access paths if a gap surfaces).

Acceptance: every tenant-scoped endpoint refuses a membership-less user; only onboarding endpoints respond; `grep` audit shows no unguarded membership access remains.

### Phase 9 — Current-membership read endpoint (gated-state signal)

> **Added 2026-06-04**: Phases 1–8 *enforce* the gate but ship no way for the frontend to *read* it. There is no endpoint returning the caller's organization/role (`OrganizationViewSet` is NoList; `/profile/me/` has no membership; the allauth session `User` has none). Without it the frontend can't distinguish "authenticated + no membership" (gated) from "onboarded", which the onboarding routing depends on. This phase adds the read signal.

**Goal**: an authenticated user (gated or onboarded) can read their current organization + role in one call, and a gated user gets an unambiguous "no tenant" signal.

**Feature flag**: none — additive new surface.

Changes:
1. @organizations/views.py: add a `current` action to `OrganizationViewSet` (`@action(detail=False, methods=["get"], url_path="current")`) with **`permission_classes=[IsAuthenticated]` only** — it must be reachable by a membership-less user AND by a member (so NOT behind `OrganizationManagementPermission`, which blocks members from the default actions). Returns the caller's organization + their role when `user.organization_membership` exists; returns **HTTP 404** when the user has no membership (the gated signal). Guard membership access with `getattr(user, "organization_membership", None)`.
2. @organizations/serializers.py: a small read serializer (e.g. `CurrentMembershipSerializer`) exposing `{ role, organization: { id, name, ... } }` (reuse `OrganizationSerializer` for the nested org). Read-only.
3. @organizations/routes.py / drf-spectacular: regenerate `schema.yml` so the new path is documented; add an `extend_schema` with a clear summary and the 200/404 responses.

Spec use-case: enables the frontend side of Use-cases 1–4 + the hard gate (read counterpart to Phase 8's enforcement).

Tests:
- **Integration**: @organizations/tests/test_views.py — onboarded ADMIN → 200 with `{role: "admin", organization: {...}}`; onboarded MEMBER → 200 with `role: "member"`; **gated (membership-less) user → 404** (the signal), not 500; unauthenticated → 401. Assert a member is NOT blocked (regression against `OrganizationManagementPermission`).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single read action + serializer + schema regen against existing precedent.

**Reusable skills**: `create-rest-endpoint` (viewset action + serializer + route + `schema.yml` regen).

Acceptance: `GET /organizations/current/` returns the caller's org + role for members (200) and 404 for membership-less users; reachable by both; `schema.yml` documents it.

## 6. Risk & Rollout Notes

- **No feature flag.** Each phase is independently mergeable and leaves the system working: Phases 1–2 are inert scaffolding; Phases 3–7 each light up one use-case; Phase 8 tightens the gate. If a phase regresses, revert that phase alone.
- **Migration safety.** Only one schema change: `Profile.pending_organization_name`, a nullable additive column on a non-hot table — no lock or rewrite concern. Reverse migration drops the column; no backfill needed (blank default).
- **Provisioning atomicity.** Email path: user/profile commit at signup; org/membership commit at confirmation — two moments by design (verification deferral). The `pending_organization_name` carries intent across them; if confirmation-time provisioning fails, the user stays gated (recoverable, not corrupt). Wrap the confirmation-hook provisioning so a failure doesn't leave a half-created org without a membership.
- **Idempotency / concurrency.** `provision_tenant_for_user` and `accept_invitation` guard the `OneToOne`; re-fired confirmation signals, social re-logins, and create/accept races all resolve to one membership with a typed rejection for the loser (Phase 1 tests assert this).
- **Backfill.** None required — existing orphaned users (if any in production) are out of scope here; they can use the existing create-org endpoint. Note if a backfill is later wanted, it would reuse `provision_tenant_for_user`.
- **Rollback.** Pure code revert per phase plus the single column drop. No data migration to unwind.
- **Abandoned onboarding.** A social user who never creates an org stays gated indefinitely (acceptable per spec). Ensure re-authentication returns them to the gated state, not a blocked blank — covered in Phase 5/6 tests.

## 7. Open Questions

1. **Allauth confirmation hook mechanics.** Default: use the `email_confirmed` signal; if code-based verification (`ACCOUNT_EMAIL_VERIFICATION_BY_CODE_ENABLED`) doesn't emit it reliably in the headless flow, fall back to overriding the confirmation method on `AccountAdapter`. Owner: eng (resolve during Phase 3). Unblocks: Phase 3 hook site.
2. **Gate scope for account-level self-service.** Default: a gated (membership-less) user may still manage their own account and sign out; only tenant-scoped surfaces are blocked. Owner: product/eng. Unblocks: Phase 8 audit boundary.
3. **Stale invitation after already-member rejection.** Default: leave the invitation pending for an admin to revoke manually; no proactive notification to the inviter. Owner: product. Unblocks: Phase 7 wording.

## 8. Touch List

**Phase 1 — provisioning service**
- [organizations/exceptions.py](../organizations/exceptions.py) — add `UserAlreadyHasMembershipError`.
- [organizations/services.py](../organizations/services.py) — add `provision_tenant_for_user`; harden `accept_invitation`.
- [organizations/tests/test_services.py](../organizations/tests/test_services.py) — branch + concurrency coverage.

**Phase 2 — capture org name at signup**
- [users/models.py](../users/models.py) — add `Profile.pending_organization_name`.
- @users/migrations/ — new migration (via `add-migration`).
- [accounts/base_forms.py](../accounts/base_forms.py) — add `organization_name` field + capture in `signup()`.
- [accounts/tests/](../accounts/tests/) — form capture + invite-skip tests.

**Phase 3 — create org on email verification** *(amended 2026-06-04)*
- [accounts/account_adapters.py](../accounts/account_adapters.py) — override `AccountAdapter.confirm_email` to provision via `provision_tenant_for_user` (pass `pending_organization_name`); clear the name on success; swallow `UserAlreadyHasMembershipError`.
- @accounts/signals.py (DELETE) + [accounts/apps.py](../accounts/apps.py) — remove the `email_confirmed` handler + its `ready()` connection.
- [accounts/tests/](../accounts/tests/) — drive confirmation via `AccountAdapter.confirm_email`: uninvited confirm → ADMIN org; idempotent re-confirm; blank-name → gated.

**Phase 4 — auto-join on email verification** *(amended 2026-06-04, test-only)*
- [accounts/tests/](../accounts/tests/) — invited confirm → MEMBER; invite-wins-over-name. Test helper drives `AccountAdapter.confirm_email`, not `verify_email`.

**Phase 5 — gated uninvited social**
- [accounts/account_adapters.py](../accounts/account_adapters.py) — confirm `save_user` leaves uninvited social users membership-less.
- [accounts/tests/](../accounts/tests/), [organizations/tests/test_views.py](../organizations/tests/test_views.py) — gated → create-org → ADMIN; second-create rejected.

**Phase 6 — auto-join on social signup**
- [accounts/account_adapters.py](../accounts/account_adapters.py) — call `provision_tenant_for_user` in `save_user`.
- [accounts/tests/](../accounts/tests/) — invited social → MEMBER; re-login no-op.

**Phase 7 — reject already-member accept**
- [organizations/views.py](../organizations/views.py), [organizations/serializers.py](../organizations/serializers.py) — map `UserAlreadyHasMembershipError` to a clear 4xx.
- [organizations/tests/test_views.py](../organizations/tests/test_views.py) — already-member accept → error, no state change.

**Phase 8 — hard-gate audit**
- Cross-app: [calendar_integration/](../calendar_integration/), [payments/](../payments/), [webhooks/](../webhooks/), [public_api/](../public_api/), [organizations/views.py](../organizations/views.py) — patch unguarded `organization_membership` access.
- [organizations/models.py](../organizations/models.py) — document the invariant near `OrganizationMembership`.
- Per-surface integration tests asserting membership-less refusal.

**Phase 9 — current-membership read endpoint** *(added 2026-06-04)*
- [organizations/views.py](../organizations/views.py) — `current` action on `OrganizationViewSet` (`IsAuthenticated` only; 200 org+role / 404 gated).
- [organizations/serializers.py](../organizations/serializers.py) — `CurrentMembershipSerializer` (role + nested org).
- @schema.yml — regenerate to document `GET /organizations/current/`.
- [organizations/tests/test_views.py](../organizations/tests/test_views.py) — admin/member 200, gated 404, member-not-blocked.

## Amendments

- **2026-06-04** — Reworked the email-confirmation provisioning mechanism from an `email_confirmed` **signal handler** to an imperative **`AccountAdapter.confirm_email` override** (Option A), for step-debuggability and symmetry with the social adapter's `save_user`. Per-adapter provisioning methods (no shared helper). Affected phases: 3 (production rewrite — delete `accounts/signals.py` + `ready()` wiring, add the adapter override), 4 (test-only — helper drives `confirm_email`). Branches force-pushed: `plan/auth-tenant-provisioning/phase-3`, `phase-4`, and rebase-only `phase-5`, `phase-6`, `phase-7`, `phase-8`.
- **2026-06-04** — Appended **Phase 9 — current-membership read endpoint** (`GET /organizations/current/`). Phases 1–8 enforced the hard gate but shipped no way for the frontend to *read* the gated state (no endpoint exposes the caller's org/role). Append-only — no rewrite of existing phases; stacked on `phase-8`.
