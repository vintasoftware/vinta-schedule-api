# REST API Frontend Gaps — Implementation Plan

Closes the private REST API gaps surfaced by the frontend use-case audit (2026-06-04). The new admin/end-user frontend needs ~10 operations that exist as service methods but are not exposed over REST, plus two small model additions. This plan exposes each via the project's `create-rest-endpoint` pattern, one use-case per phase.

No sibling SPEC doc — this plan derives from the audit conducted in-conversation and the decisions confirmed via interrogation (see **Guiding Decisions**).

## 1. Goals

1. Expose every audited admin operation over private REST: list team members, deactivate a member, trigger room/resource sync, manually sync another user's calendar, transfer an event between calendars, manage public-API (GraphQL) tokens.
2. Expose every audited end-user operation over private REST: request own calendar import, request own calendar sync, soft-disable own calendar, update/disable a calendar bundle, fetch date-range-expanded events for calendar grids.
3. Gate all admin-only endpoints behind a single reusable `IsOrganizationAdmin` DRF permission so the rule has one implementation.
4. Keep the multi-tenant hard-gate invariant intact: membership-less users get clean refusals, never 500s; every new queryset is organization-scoped.

**Non-goals:**
- No frontend code. This plan is backend REST surface only; the frontend is specced separately against this surface.
- No new business logic — every phase wires an *existing* service method to REST (exceptions: `OrganizationMembership.is_active` and `Calendar.is_active` field additions, which are storage, not logic).
- No public GraphQL changes. Token *management* lands on private REST (admin UI consumer); the public GraphQL `check_token` mutation stays as-is.
- No `should_sync_rooms` write work — it is already writable on `OrganizationViewSet` create+update. Only the "trigger sync when enabled" convenience lands here.
- No global user deactivation. "Disable a user" is tenant-scoped membership deactivation only.
- No calendar hard-delete change beyond redirecting `destroy` to soft-disable.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **No feature flags** | The project has no feature-flag system (no waffle/flipper/`is_enabled`; confirmed by search). Every phase is additive new surface (new endpoint/action/field no existing caller reads) except the `Calendar.destroy` redirect — handled by making the field default-active and adding the inactive filter so pre-feature reads are byte-for-byte unchanged. No flag added, no flag-removal phase. |
| **Admin gating** | New `IsOrganizationAdmin(BasePermission)` in `organizations/permissions.py`, delegating to the existing `User.is_organization_admin(organization_id)` method (already used by `CalendarPermissionService.can_manage_calendar_group`, [calendar_permission_service.py:344](../calendar_integration/services/calendar_permission_service.py)). Single implementation, reused by all admin endpoints. |
| **Disable user = deactivate membership** | Add `OrganizationMembership.is_active` (default `True`). Disable sets it `False`; tenant-scoped, reversible, preserves role/history. The hard-gate already refuses membership-less users — an inactive membership is treated as gated by the queryset scoping. Chosen over global `User.is_active` (too broad — cross-tenant/login) and over row deletion (loses role/audit). |
| **Token management surface** | Private REST `/public-api-tokens/`, admin-only, consumed by the admin frontend. Create returns the plaintext token once (mirrors `create_system_user` returning `(SystemUser, token)`); list never returns it; revoke flips `SystemUser.is_active`. Chosen over GraphQL mutations (inconsistent with the REST admin UI) and over Django-admin-only (frontend can't manage). |
| **Calendar soft-disable** | Add `Calendar.is_active` (default `True`). `destroy` is overridden to set inactive instead of deleting. Default list/detail querysets filter `is_active=True`; `?include_inactive=true` opt-in surfaces disabled rows. Calendar has no partitioning ([Calendar.Meta](../calendar_integration/models.py) only sets `unique_together`), so the column add is a cheap additive migration. |
| **Bundle disable reuses calendar soft-disable** | A bundle *is* a `Calendar` (`calendar_type=BUNDLE`). Disabling a bundle = `Calendar.is_active=False` plus cascading the bundle's non-primary representations. No separate bundle flag. |
| **DI pattern for services** | All actions/permissions use the established `@inject` + `Annotated[Service, Provide["service_name"]]` dependency-injector pattern (see [available_windows action, views.py:132](../calendar_integration/views.py)). |
| **Org scoping** | Every new queryset follows the existing `get_queryset` pattern: resolve `user.organization_membership.organization_id`, `.filter_by_organization(org_id)`, return `Model.original_manager.none()` for membership-less users. |
| **Schema** | Every REST phase regenerates `schema.yml` via `make update_schema` (`python manage.py spectacular --color --file schema.yml`). |
| **Plan location** | `ai-plans/` per repo convention (recent commits + existing files). Supersedes the stale `dev-plans/` memory note for this repo. |

## 3. Data Model Changes

### 3.1 `OrganizationMembership.is_active`
New `BooleanField(default=True, db_index=True)` on [organizations/models.py](../organizations/models.py) `OrganizationMembership` (currently fields: `user` OneToOne, `organization` FK, `role`). Additive migration. Inactive membership = gated user (no tenant access) without losing the row.

### 3.2 `Calendar.is_active`
New `BooleanField(default=True, db_index=True)` on [calendar_integration/models.py](../calendar_integration/models.py) `Calendar`. Additive migration; table is non-partitioned. Default-active keeps every existing read unchanged until the inactive filter ships in the same phase.

### 3.3 Type plumbing
- `OrganizationMembershipSerializer` (new) + nested user/profile read fields for the team datatable.
- `SystemUserTokenSerializer` / `SystemUserTokenCreateSerializer` / `SystemUserTokenUpdateSerializer` (new) exposing `integration_name`, `is_active`, `available_resources` (list of `PublicAPIResources` values), and a write-once `token` field on create. The update serializer accepts `available_resources` only (never re-issues the secret).
- `CalendarBundleUpdateSerializer` (new) accepting `child_calendars` + `primary_calendar` ids.
- `EventExpandedSerializer` (reuse existing event serializer; the action returns a flat list of materialized occurrences).

## 4. API Design

All paths are under the API namespace (`DefaultRouter`, `use_regex_path=False`). Routes registered in each app's `routes.py` (`RouteDict` list) per [organizations/routes.py](../organizations/routes.py) / [calendar_integration/routes.py](../calendar_integration/routes.py).

### 4.1 Organization members (admin)
- `GET /organization-members/` — list active+inactive memberships for caller's org (datatable). Admin.
- `GET /organization-members/{id}/` — retrieve. Admin.
- `POST /organization-members/{id}/deactivate/` — set `is_active=False`. Admin.
- `POST /organization-members/{id}/reactivate/` — set `is_active=True`. Admin.

### 4.2 Calendar sync/import (`@action` on `CalendarViewSet`)
- `POST /calendar/request-import/` — caller imports their own external calendars (`request_calendars_import`). Authenticated.
- `POST /calendar/{id}/request-sync/` — caller syncs an owned calendar (`request_calendar_sync`, body: `start_datetime`, `end_datetime`, `should_update_events?`). Authenticated; object-scoped to owner.
- `POST /calendar/{id}/admin-sync/` — admin syncs any calendar in the org. Admin.

### 4.3 Organization room sync (admin)
- `POST /organizations/{id}/sync-rooms/` — trigger `request_organization_calendar_resources_import` (body: `start_time`, `end_time`). Admin. (Config flag `should_sync_rooms` already writable via existing create/update.)

### 4.4 Event transfer (admin)
- `POST /calendar-events/{id}/transfer/` — body `target_calendar_id`; calls `transfer_event`. Admin.

### 4.5 Calendar soft-disable
- `DELETE /calendar/{id}/` — overridden to set `is_active=False` (no row delete).
- `GET /calendar/?include_inactive=true` — opt-in to see disabled calendars.

### 4.6 Calendar bundle
- `PATCH /calendar/{id}/bundle/` — update bundle children/primary (existing `POST /calendar/bundle/` creates).
- Bundle disable: `DELETE /calendar/{id}/` on a `BUNDLE` calendar cascades to representations.

### 4.7 Public-API tokens (admin)
- `GET /public-api-tokens/` — list org's `SystemUser`s (no plaintext token). Admin.
- `POST /public-api-tokens/` — create `SystemUser` + `ResourceAccess` rows; returns plaintext token once. Admin.
- `PATCH /public-api-tokens/{id}/` — replace the token's `ResourceAccess` grants (no new secret). Admin.
- `POST /public-api-tokens/{id}/revoke/` — set `SystemUser.is_active=False`. Admin.

### 4.8 Events expanded
- `GET /calendar-events/expanded/?calendar_id=&start_datetime=&end_datetime=` — flat list of occurrences (recurring materialized, exceptions merged) for grid views. Authenticated.

## 5. Phased Rollout

### Phase 0 — Reusable `IsOrganizationAdmin` permission

**Goal**: one DRF permission class gating all admin-only endpoints; no behavior change on its own.

**Feature flag**: none — additive scaffolding, no existing caller.

Changes:
1. [organizations/permissions.py](../organizations/permissions.py): add `IsOrganizationAdmin(BasePermission)`. `has_permission` → authenticated + `getattr(user, "organization_membership", None)` active. `has_object_permission` → `obj` org matches membership org AND `user.is_organization_admin(membership.organization_id)`. Model on `CalendarGroupPermission` structure ([calendar_integration/permissions.py:43](../calendar_integration/permissions.py)).
2. Export if the app indexes permissions.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: `organizations/tests/test_permissions.py` — admin allowed, member denied, membership-less denied, inactive membership denied, cross-org denied.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single permission class, clear precedent.

**Reusable skills**: none.

Acceptance: `IsOrganizationAdmin` returns True only for active admins of the object's org; unit tests green.

---

### Phase 1 — Add `OrganizationMembership.is_active`

**Goal**: storage for tenant-scoped member deactivation; no user-visible behavior yet.

**Feature flag**: none — additive column, default `True` (every existing read unchanged).

Changes:
1. [organizations/models.py](../organizations/models.py): add `is_active = BooleanField(default=True, db_index=True)` to `OrganizationMembership`.
2. Migration (additive, non-partitioned table).
3. Update the hard-gate read path: wherever `user.organization_membership` resolves a member for tenant access, treat `is_active=False` as gated (queryset scoping returns `none()`). Audit the `get_queryset` org-resolution sites touched by commit `3c51962`.

Spec use-case: shared scaffolding for members/disable phases.

Tests:
- **Unit**: `organizations/tests/test_models.py` — default `True`; factory supports `is_active=False`.
- **Integration**: assert an inactive membership is treated as gated at a representative tenant-scoped endpoint (empty queryset / refusal, not 500).

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano` / `gemini-2.5-flash-lite`. Single-field migration.

**Reusable skills**: `add-migration`.

Acceptance: migration applies; inactive membership yields a clean refusal at tenant endpoints; tests green.

---

### Phase 2 — List organization members (admin)

**Goal**: admin can list their team in a datatable.

**Feature flag**: none — new endpoint.

Changes:
1. `OrganizationMembershipViewSet` (list + retrieve) using `ReadOnlyVintaScheduleModelViewSet`; `get_queryset` org-scoped; `permission_classes = [IsOrganizationAdmin]`.
2. `OrganizationMembershipSerializer` with nested user email + profile name + `role` + `is_active`.
3. Register `organization-members` in [organizations/routes.py](../organizations/routes.py).
4. `make update_schema`.

Spec use-case: "Admin lists their team in a datatable."

Tests:
- **Unit**: serializer shape.
- **Integration**: `organizations/tests/test_views.py` — admin lists own-org members (active+inactive); member forbidden; cross-org rows excluded.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `GET /organization-members/` returns the caller-org members for an admin, 403 for a member.

---

### Phase 3 — Deactivate / reactivate a member (admin)

**Goal**: admin can disable (and re-enable) a team member.

**Feature flag**: none — new actions.

Changes:
1. Add `deactivate` + `reactivate` `@action`s to `OrganizationMembershipViewSet` flipping `is_active`. Admin via `IsOrganizationAdmin`.
2. Guard: admin cannot deactivate their own membership (avoid self-lockout) and cannot deactivate the last active admin.
3. `make update_schema`.

Spec use-case: "Admin disables a user."

Tests:
- **Integration**: deactivate flips flag + gates the target; reactivate restores; self-deactivation blocked; last-admin guard; member forbidden.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /organization-members/{id}/deactivate/` gates that member; guards enforced; tests green.

---

### Phase 4 — Request own calendar import

**Goal**: user triggers import of their external calendars.

**Feature flag**: none — new action.

Changes:
1. `request_import` `@action` (POST, detail=False) on `CalendarViewSet`; `@inject` `CalendarService`; authenticate with caller's `SocialAccount` (pattern from `available_windows`); call `request_calendars_import()`.
2. `make update_schema`.

Spec use-case: "User requests a calendar to sync" (import half).

Tests:
- **Integration**: authenticated user triggers import (service called, 202/200); unauthenticated 401.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /calendar/request-import/` invokes `request_calendars_import` for the caller.

---

### Phase 5 — Request own calendar sync

**Goal**: user syncs one of their calendars over a date range.

**Feature flag**: none — new action.

Changes:
1. `request_sync` `@action` (POST, detail=True) on `CalendarViewSet`; body `start_datetime`, `end_datetime`, `should_update_events?`; object-scoped to an owned calendar; call `request_calendar_sync(calendar, start, end, should_update_events)`.
2. ISO datetime validation (reuse `available_windows` parsing).
3. `make update_schema`.

Spec use-case: "User requests a calendar to sync" (sync half).

Tests:
- **Integration**: owner syncs → `CalendarSync` requested; non-owner forbidden; bad datetimes 400.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /calendar/{id}/request-sync/` returns a requested `CalendarSync` for the owner.

---

### Phase 6 — Admin syncs another user's calendar

**Goal**: admin manually syncs any org calendar.

**Feature flag**: none — new action.

Changes:
1. `admin_sync` `@action` (POST, detail=True) on `CalendarViewSet`, `IsOrganizationAdmin`; resolves the target calendar within the admin's org (not just owned); calls `request_calendar_sync`.
2. `make update_schema`.

Spec use-case: "Admin manually syncs another user's calendar."

Tests:
- **Integration**: admin syncs another user's org calendar; member forbidden; cross-org calendar 404.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /calendar/{id}/admin-sync/` syncs any in-org calendar for an admin; 403 for members.

---

### Phase 7 — Trigger organization rooms/resources sync (admin)

**Goal**: admin triggers room/resource import; optional auto-trigger when `should_sync_rooms` is enabled.

**Feature flag**: none — new action; the `should_sync_rooms` field is already writable.

Changes:
1. `sync_rooms` `@action` (POST, detail=True) on `OrganizationViewSet`, `IsOrganizationAdmin`; body `start_time`, `end_time`; call `request_organization_calendar_resources_import(start, end)`.
2. Optional: when `OrganizationViewSet` update flips `should_sync_rooms` `False→True`, enqueue the import once (document as part of this phase, guarded to fire only on the transition).
3. `make update_schema`.

Spec use-case: "Admin triggers a rooms sync" (and supports "configure org to sync rooms", already writable).

Tests:
- **Integration**: admin triggers import; member forbidden; enabling `should_sync_rooms` via PATCH fires import exactly once on the transition.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Transition-detection branch + action across two viewsets.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /organizations/{id}/sync-rooms/` invokes the resource import for an admin; transition auto-trigger fires once.

---

### Phase 8 — Transfer event between calendars (admin)

**Goal**: admin moves an event from one user's calendar to another.

**Feature flag**: none — new action.

Changes:
1. `transfer` `@action` (POST, detail=True) on `CalendarEventViewSet`, `IsOrganizationAdmin`; body `target_calendar_id` (resolved within org); call `transfer_event(event, target_calendar)`; return updated event.
2. `make update_schema`.

Spec use-case: "Admin transfers an event from one calendar to another."

Tests:
- **Integration**: admin transfers in-org event to in-org target; member forbidden; cross-org target rejected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Service coordination + provider-sync side effects.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /calendar-events/{id}/transfer/` relocates the event for an admin; 403 for members.

---

### Phase 9 — Calendar soft-disable

**Goal**: user disables a calendar without losing data; disabled calendars hidden by default.

**Feature flag**: none — column default-active + same-phase filter keep pre-feature reads byte-for-byte identical.

Changes:
1. [calendar_integration/models.py](../calendar_integration/models.py): add `Calendar.is_active = BooleanField(default=True, db_index=True)` + migration.
2. `CalendarViewSet.get_queryset`: filter `is_active=True` unless `?include_inactive=true`.
3. Override `destroy`/`perform_destroy` to set `is_active=False` instead of deleting.
4. `make update_schema`.

Spec use-case: "User disables a calendar of their own."

Tests:
- **Unit**: default `True`; manager/queryset filter helper.
- **Integration**: `DELETE /calendar/{id}/` flips inactive (row persists); default list hides it; `?include_inactive=true` shows it; flag-default read on pre-existing rows unchanged.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Field + destroy override + queryset filter across one viewset.

**Reusable skills**: `add-migration`, `create-rest-endpoint`.

Acceptance: deleting a calendar disables it; default listing omits it; opt-in surfaces it.

---

### Phase 10 — Update calendar bundle

**Goal**: admin/owner edits a bundle's child calendars + primary.

**Feature flag**: none — new action (existing `POST /calendar/bundle/` creates).

Changes:
1. `bundle` `@action` (PATCH, detail=True) on `CalendarViewSet`; `CalendarBundleUpdateSerializer` (`child_calendars`, `primary_calendar`); reconcile `ChildrenCalendarRelationship` rows for a `BUNDLE` calendar (add/remove children, set `is_primary`).
2. Reject if calendar is not a bundle; validate children are in-org.
3. `make update_schema`.

Spec use-case: "Admin updates a calendar bundle."

Tests:
- **Integration**: add/remove children; change primary; non-bundle target 400; out-of-org child rejected.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Relationship reconciliation logic.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `PATCH /calendar/{id}/bundle/` reconciles children/primary for a bundle calendar.

---

### Phase 11 — Disable calendar bundle

**Goal**: admin disables a bundle and its representations.

**Feature flag**: none — builds on Phase 9 soft-disable.

Changes:
1. Extend the Phase 9 `destroy` override: when the calendar is `BUNDLE`, cascade `is_active=False` to its non-primary representations / linked blocked-times as appropriate (consistent with `_delete_bundle_event` cleanup semantics).
2. `make update_schema` (no new route; behavior change to existing destroy on bundles).

Spec use-case: "Admin disables a calendar bundle."

Tests:
- **Integration**: disabling a bundle hides the bundle + cascades representations; primary child handling matches expected semantics.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Bundle cascade semantics.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: deleting a bundle calendar disables it and its representations; tests green.

---

### Phase 12 — Create public-API token (admin)

**Goal**: admin generates a GraphQL API token with resource permissions.

**Feature flag**: none — new endpoint.

Changes:
1. `SystemUserTokenViewSet` (create only this phase) using a `NoListVintaScheduleModelViewSet`-style base or `GenericViewSet`; `IsOrganizationAdmin`; org-scoped.
2. `SystemUserTokenCreateSerializer`: `integration_name`, `available_resources` (list validated against `PublicAPIResources`). On create → `PublicAPIAuthService.create_system_user(integration_name, organization)`, persist `ResourceAccess` rows, return plaintext `token` **once**.
3. Register `public-api-tokens` route (new `public_api/routes.py`, included in [urls.py](../vinta_schedule_api/urls.py)).
4. `make update_schema`.

Spec use-case: "Admin generates a new GraphQL API token with permissions."

Tests:
- **Integration**: admin creates token → plaintext returned once + `ResourceAccess` rows created; invalid resource 400; member forbidden; token org-scoped.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New app route wiring + write-once token + permission rows.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /public-api-tokens/` returns a one-time token and persists the requested resource grants for an admin.

---

### Phase 13 — List public-API tokens (admin)

**Goal**: admin sees existing tokens (without secrets).

**Feature flag**: none — new action.

Changes:
1. Add `list` + `retrieve` to `SystemUserTokenViewSet`; `SystemUserTokenSerializer` exposes `integration_name`, `is_active`, `available_resources` — never the token hash/plaintext.
2. `make update_schema`.

Spec use-case: supports token management UI (read side of generate/invalidate).

Tests:
- **Integration**: admin lists org tokens; no secret field present; member forbidden; cross-org excluded.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `GET /public-api-tokens/` lists org tokens with no secret leakage.

---

### Phase 14 — Revoke public-API token (admin)

**Goal**: admin invalidates a token.

**Feature flag**: none — new action.

Changes:
1. `revoke` `@action` (POST, detail=True) on `SystemUserTokenViewSet`; set `SystemUser.is_active=False`; `IsOrganizationAdmin`.
2. `make update_schema`.

Spec use-case: "Admin invalidates a GraphQL API token."

Tests:
- **Integration**: revoke flips `is_active` and subsequent `check_system_user_token` fails; member forbidden; cross-org 404.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /public-api-tokens/{id}/revoke/` deactivates the token; auth checks then reject it.

---

### Phase 15 — Edit public-API token permissions (admin)

**Goal**: admin changes a token's resource grants without re-issuing the secret.

**Feature flag**: none — new action.

Changes:
1. Add `update`/`partial_update` to `SystemUserTokenViewSet`; `SystemUserTokenUpdateSerializer` accepts `available_resources` only and reconciles `ResourceAccess` rows for the `SystemUser` (add missing, remove dropped). `integration_name`/token never mutated; `IsOrganizationAdmin`.
2. Validate each grant against `PublicAPIResources`; org-scope the target token.
3. `make update_schema`.

Spec use-case: "Admin edits a GraphQL API token's permissions."

Tests:
- **Integration**: PATCH replaces grants (add + remove reflected in `ResourceAccess`); secret unchanged and not returned; invalid resource 400; member forbidden; cross-org 404.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Grant reconciliation mirrors the create-phase write path.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `PATCH /public-api-tokens/{id}/` updates the token's resource grants; secret untouched; tests green.

---

### Phase 16 — Expanded events action

**Goal**: month/week calendar grids fetch materialized occurrences over a date range.

**Feature flag**: none — new action.

Changes:
1. `expanded` `@action` (GET, detail=False) on `CalendarEventViewSet`; params `calendar_id`, `start_datetime`, `end_datetime`; `@inject` `CalendarService`; call `get_calendar_events_expanded(calendar, start, end)` (recurring instances materialized, exceptions merged); serialize as flat list.
2. Org/owner scoping on `calendar_id`; ISO validation.
3. `make update_schema`.

Spec use-case: "User displays their calendar events in a list or big calendar view (month/week)."

Tests:
- **Integration**: range with a recurring series returns expanded instances + applies exceptions; non-owner/out-of-org calendar rejected; bad datetimes 400.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Recurring expansion correctness.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `GET /calendar-events/expanded/` returns materialized occurrences across the range for the owner.

---

## Amendment (2026-06-05) — follow-up gaps after first pass

Two gaps surfaced after the initial 17 phases shipped: (1) no way to **resend** a pending invitation, and (2) rooms-syncing is not actually usable — Phase 7's `request_rooms_sync` (and the pre-existing `create_organization` path) call `initialize_without_provider` (account=None) before `request_organization_calendar_resources_import`, which requires a provider-authenticated service and therefore **raises at runtime**; and there is no API to configure the org's `GoogleCalendarServiceAccount` that the resource import needs. Phases 17–19 close these.

### Phase 17 — Resend organization invitation (admin)

**Goal**: an admin can resend a pending invitation (regenerate token, extend expiry, re-send email).

**Feature flag**: none — new action.

Changes:
1. `resend` `@action` (POST, detail=True) on `OrganizationInvitationViewSet`. Resolve the invitation via `get_object()` (org-scoped); refuse if already accepted. Call `OrganizationService.invite_user_to_organization(...)` with the invitation's email/first/last/org and the requesting user as `invited_by` — the service already resets token+expiry and re-sends the email (get_or_create reset path).
2. Gate to admins (`OrganizationInvitationPermission` currently allows any active member to manage invitations; resend is a management action — keep consistent with how invitations are created today, i.e. members with an active membership; if invitation creation is admin-only in practice, match that). Do not change create/destroy gating.
3. `make update_schema`.

Spec use-case: "Admin resends a pending invitation."

Tests:
- **Integration**: resend a pending invite → 200, token_hash changed, expires_at extended, email re-sent (assert notification service called); resend an accepted invite → 400; cross-org invite → 404; non-member → 403.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /invitations/{id}/resend/` regenerates + re-sends a pending invitation.

### Phase 18 — Configure organization Google service account (admin)

**Goal**: an admin can configure the org's `GoogleCalendarServiceAccount` (the credentials the rooms/resource import authenticates with).

**Feature flag**: none — new endpoint.

**Security note**: this endpoint accepts a Google service-account **private key**, stored via the model's `EncryptedCharField`. Write-only for secret fields (never returned in any response); admin-only; org-scoped. Treat as a credentials surface.

Changes:
1. `GoogleServiceAccountViewSet` (create + retrieve/update; no list needed, one per org/resource) or a dedicated serializer-driven endpoint, `IsOrganizationAdmin`, org-scoped. Accepts `email`, `audience`, `public_key`, `private_key_id`, `private_key` (last two write-only). Response exposes only non-secret fields (e.g. `id`, `email`, `audience`, configured-at) — NEVER `private_key`/`private_key_id`.
2. Route registration in `calendar_integration/routes.py`; `make update_schema`.

Spec use-case: "Admin configures their organization to sync rooms (service account credentials)."

Tests:
- **Integration**: admin creates/sets the service account → 201/200, secret fields stored (encrypted) but NEVER echoed in the response; update rotates credentials; non-admin → 403; cross-org → 404; response contains no private key.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Credentials surface + write-only secret handling.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /…/google-service-account/` stores the org's service-account credentials; secrets never returned.

### Phase 19 — Authenticate rooms-sync with the service account (fix the trigger)

**Goal**: make `request_rooms_sync` (and the `create_organization` rooms path) actually run — authenticate the service with the org's `GoogleCalendarServiceAccount` before `request_organization_calendar_resources_import`.

**Feature flag**: none — bug fix on existing (currently-raising) paths.

Changes:
1. `OrganizationService.request_rooms_sync`: resolve the org's `GoogleCalendarServiceAccount`; if present, `calendar_service.authenticate(account=<service_account>, organization=org)` (NOT `initialize_without_provider`) then `request_organization_calendar_resources_import(...)`. If no service account is configured → raise a clear, catchable error so the `sync_rooms` action returns **400** ("configure a Google service account first") rather than 500.
2. `create_organization(should_sync_rooms=True)`: route through the same fixed path; if no service account exists at creation time, do NOT crash — skip/queue gracefully (document the chosen behavior; org creation must succeed).
3. `sync_rooms` action + transition: surface the "no service account" case as 400.
4. `make update_schema` (if responses change).

Spec use-case: "Admin triggers a rooms sync (now actually executes)."

Tests:
- **Integration**: with a configured service account, `POST /organizations/{id}/sync-rooms/` authenticates with it (assert authenticate called with the GoogleCalendarServiceAccount) and enqueues the import; without one → 400 (not 500); create_organization(should_sync_rooms=True) with no service account does not crash; transition False→True with a service account triggers.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Cross-service auth wiring + fixing a live break.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: rooms sync runs when a service account is configured; returns 400 (never 500) when it isn't; org creation never crashes.

## 6. Risk & Rollout Notes

- **No feature flags** (none exist in project). Safety comes from additive design: new routes/actions/fields with `default=True`, plus same-phase read filters so pre-existing data reads unchanged. The only behavior change to an existing endpoint is `Calendar.destroy` (Phase 9) and bundle destroy cascade (Phase 11) — each ships a test asserting prior data stays intact (rows persist as inactive).
- **Migrations** (Phases 1, 9): two additive boolean columns on non-partitioned tables (`OrganizationMembership`, `Calendar`), each `default=True` + `db_index=True`. No table rewrite risk; index build is cheap at expected cardinality. Use `add-migration` skill.
- **Hard-gate invariant**: Phase 1 must extend the membership-active check to every org-resolution site touched by commit `3c51962` — missing one means an inactive member retains access. Phase 1 integration test guards this.
- **Provider side effects**: Phases 5/6 (`request_calendar_sync`), 7 (resource import), 8 (`transfer_event`) call services that hit external providers (Google/Microsoft) asynchronously. Endpoints should return promptly (request-accepted) and not block on provider round-trips.
- **Token secret hygiene** (Phases 12–14): plaintext token returned exactly once on create; list/retrieve serializers must never include the hash or plaintext. Covered by an explicit "no secret field" integration assertion.
- **Self-lockout / last-admin** (Phase 3): guards prevent an admin disabling themselves or the final active admin.
- **Rollback**: each phase is an independent PR; revert the PR. Migration phases roll back by reversing the additive column (no data loss since columns are nullable-equivalent with defaults). No flag to flip.
- **Ordering**: Phase 0 (permission) and Phase 1 (membership field) are foundations and must merge before their dependents (Phases 2–3 depend on both; admin Phases 6–8, 12–14 depend on Phase 0; Phase 11 depends on Phase 9). All other phases are independent and parallelizable.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should `request-sync`/`admin-sync`/`sync-rooms` return the created `CalendarSync`/import-workflow id for client polling, or fire-and-forget 202? | Return the workflow/sync id + status so the frontend can poll. | Eng |
| On member reactivation, restore prior `role` (preserved on the row) or reset to `MEMBER`? | Preserve prior role (row was never deleted). | Product |
| Bundle disable (Phase 11): hard-cancel future bundle events on the representations, or leave them and only hide the bundle? | Leave events, hide bundle; surface a follow-up if cancellation is desired. | Product |

## 8. Touch List

**Phase 0** — Edit [organizations/permissions.py](../organizations/permissions.py); new `organizations/tests/test_permissions.py`.

**Phase 1** — Edit [organizations/models.py](../organizations/models.py); new migration `@organizations/migrations/`; edit org-resolution `get_queryset` sites; edit `organizations/tests/test_models.py`.

**Phase 2** — Edit [organizations/views.py](../organizations/views.py), [organizations/serializers.py](../organizations/serializers.py), [organizations/routes.py](../organizations/routes.py); edit `organizations/tests/test_views.py`; `schema.yml`.

**Phase 3** — Edit [organizations/views.py](../organizations/views.py); edit `organizations/tests/test_views.py`; `schema.yml`.

**Phases 4–6** — Edit [calendar_integration/views.py](../calendar_integration/views.py) (`CalendarViewSet` actions); edit `calendar_integration/tests/`; `schema.yml`.

**Phase 7** — Edit [organizations/views.py](../organizations/views.py) (`OrganizationViewSet`); edit `organizations/tests/test_views.py`; `schema.yml`.

**Phase 8** — Edit [calendar_integration/views.py](../calendar_integration/views.py) (`CalendarEventViewSet`); edit tests; `schema.yml`.

**Phase 9** — Edit [calendar_integration/models.py](../calendar_integration/models.py); new migration `@calendar_integration/migrations/`; edit [calendar_integration/views.py](../calendar_integration/views.py) (`CalendarViewSet` queryset + destroy); edit tests; `schema.yml`.

**Phase 10** — Edit [calendar_integration/views.py](../calendar_integration/views.py), `calendar_integration/serializers.py`; edit tests; `schema.yml`.

**Phase 11** — Edit [calendar_integration/views.py](../calendar_integration/views.py) (destroy override extension); edit tests; `schema.yml`.

**Phase 12** — New `public_api/routes.py`, `public_api/views.py`, `public_api/serializers.py`; edit [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py); new `public_api/tests/test_views.py`; `schema.yml`.

**Phase 13** — Edit `public_api/views.py`, `public_api/serializers.py`; edit `public_api/tests/test_views.py`; `schema.yml`.

**Phase 14** — Edit `public_api/views.py`; edit `public_api/tests/test_views.py`; `schema.yml`.

**Phase 15** — Edit `public_api/views.py`, `public_api/serializers.py`; edit `public_api/tests/test_views.py`; `schema.yml`.

**Phase 16** — Edit [calendar_integration/views.py](../calendar_integration/views.py) (`CalendarEventViewSet`); edit tests; `schema.yml`.
