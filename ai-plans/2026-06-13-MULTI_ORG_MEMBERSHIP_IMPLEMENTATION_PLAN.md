# Multi-Organization Membership + Active-Org Header — Implementation Plan

> No `..._SPEC.md` sibling exists. The decisions below were captured through a Step 0
> interrogation (recorded in **Guiding Decisions**) and stand in for the spec. Use-cases
> referenced by each phase are the numbered entries under **Use-cases** in this document.

## 1. Goals

1. A user can belong to **multiple organizations** — `OrganizationMembership.user` becomes a
   ForeignKey (was OneToOne), `unique(user, organization)`.
2. The internal REST/JWT API resolves the **active organization per request** from an
   `X-Organization-Id` header, with deterministic fallbacks and rejections.
3. The frontend can **list a user's organizations** (for a switcher) via a new endpoint.
4. A user already in one org can **join additional orgs** — by accepting an invitation, or by
   creating another org (becoming its admin).

**Non-goals:**
- Per-user multi-org switching on the **public GraphQL** surface (`public_api`). It authenticates
  via `SystemUser` machine tokens, not human memberships, and keeps its existing
  `X-Public-Api-Organization-Id` header untouched.
- A persisted "default/last-used org" preference. Header-absent resolution is implicit-or-400,
  not a stored choice (see **Open Questions**).
- Cross-org data moves, merging orgs, or per-org user profiles.
- Frontend switcher UI (this plan delivers the API the UI consumes).
- Backwards-compatibility shims / feature flag — the app is **not yet in production** (see
  **Guiding Decisions**).

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Membership cardinality** | `OrganizationMembership.user`: `OneToOneField` → `ForeignKey`, with a `UniqueConstraint(user, organization)`. One user, many orgs; at most one membership row per (user, org). Existing rows migrate 1:1 with zero data change. |
| **Reverse accessor rename** | `related_name="organization_membership"` → `related_name="organization_memberships"` (now a manager). Direct reverse reads (`users.models.is_organization_admin`, `hasattr(user, "organization_membership")` provisioning guards) are rewritten; everything routing through `get_active_organization_membership(...)` stays put. |
| **Active-org resolution seam** | A request-scoped resolver runs at the DRF view layer (a `TenantScopedViewMixin.initial()` added to the shared base viewsets in [view_utils.py](../common/utils/view_utils.py)). It resolves the header → membership and **stashes it on `request` and `request.user` (`user._active_membership`)**. `get_active_organization_membership(user)` reads the stashed value, so the ~60 existing call sites are untouched. This is the single biggest churn-avoidance lever. |
| **Why stash, not refactor 60 sites** | 128 references / 60 `get_active_organization_membership` call sites already funnel through one helper that takes the user. Re-threading `request` through all of them is a large, error-prone diff. Stashing the resolved membership on the user object preserves the helper's signature and keeps tenant-scoping correctness centralized. |
| **Header name** | `X-Organization-Id` (first-party; parallels the existing `X-Public-Api-Organization-Id`). |
| **Header present** | Must name an org the user is an **active** member of → resolve to it. Otherwise **403** (treated like no-access, not silent fallback). |
| **Header absent** | Exactly one active membership → resolve implicitly (preserves today's behavior). Two+ active memberships → **400** (`X-Organization-Id required`). Zero memberships → gated (unchanged onboarding path). |
| **Multi-org join** | `accept_invitation` / `provision_tenant_for_user` / `AcceptInvitationView` stop refusing a second membership; they refuse only a **duplicate membership in the same org**. |
| **Create additional org** | `OrganizationManagementPermission` is relaxed so an authenticated user with existing memberships may also `POST /organizations/` and become admin of the new one. |
| **No feature flag** | App is pre-production with no live tenants; the FK migration is 1:1 and single-membership users behave byte-for-byte as today. A flag would gate a hot path with no production traffic to protect — pure debt. Justified skip per the planning skill's "purely-additive / no live callers" carve-out. |
| **Public GraphQL untouched** | `public_api` middleware + `X-Public-Api-Organization-Id` unchanged. |

## 3. Data Model Changes

### 3.1 `OrganizationMembership.user` — OneToOne → ForeignKey

In [organizations/models.py](../organizations/models.py#L147-L151):

```python
user = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="organization_memberships",  # was "organization_membership"
)
# ...
class Meta:
    constraints = [
        models.UniqueConstraint(
            fields=["user", "organization"],
            name="uniq_membership_user_organization",
        ),
    ]
```

- Rewrite the **Hard-gate invariant** docstring ([organizations/models.py:131-145](../organizations/models.py#L131-L145)):
  a user is *gated* only when they have **zero** active memberships; otherwise resolution is
  header-driven. "Exactly one membership" is no longer the invariant.
- The migration is a Django `AlterField` (OneToOne→FK drops the implicit unique on `user_id`) plus
  an `AddConstraint`. On Postgres this rewrites the unique index on `user_id` into the composite
  constraint. See **Risk & Rollout Notes** for lock posture.

### 3.2 Request type plumbing

- Add `organization_membership: OrganizationMembership | None` and
  `organization: Organization | None` as documented attributes the resolver sets on the internal
  request (mirrors how `public_api` documents `public_api_organization` on its request type).
- Helper `get_active_organization_membership(user)` ([organizations/models.py:18-38](../organizations/models.py#L18-L38))
  reads `getattr(user, "_active_membership", _UNSET)`; on `_UNSET` (non-DRF entry points, mgmt
  commands, tests not going through a view) it falls back to "the user's single active membership"
  so off-request code keeps working.

## 4. API Design

### 4.1 Active-org header (all internal REST endpoints)

`X-Organization-Id: <organization uuid>` — optional. Resolution table:

| Memberships (active) | Header | Result |
|---|---|---|
| 0 | any | Gated (existing onboarding behavior; `request.organization_membership = None`) |
| 1 | absent | Resolve to that membership |
| 1 | present, matches | Resolve to it |
| 1 | present, mismatched | **403** |
| 2+ | absent | **400** `{"detail": "X-Organization-Id header required."}` |
| 2+ | present, member | Resolve to the named org |
| 2+ | present, non-member | **403** |

### 4.2 `GET /organizations/mine/`

List the caller's **active** memberships for a switcher.

- Response `200`: `[{"organization": {"id", "name"}, "role"}]` (reuses/extends
  `CurrentMembershipSerializer` shape, [organizations/serializers.py:222-234](../organizations/serializers.py#L222-L234)).
- No header required — this is how the client *discovers* which ids to send back in
  `X-Organization-Id`. Empty list for gated users (`200 []`, not 404).

### 4.3 Existing `GET /organizations/current/`

After the resolver lands, `current` ([organizations/views.py:196-207](../organizations/views.py#L196-L207))
returns the **resolved** active membership (honoring the header) instead of the single OneToOne.
404 stays for gated users.

## 5. Phased Rollout

Ordered so the schema change (slowest to reverse) lands first, then the resolver it enables, then
the thin per-use-case additions on top.

### Phase 1 — Membership FK migration

**Goal**: a user can hold multiple `OrganizationMembership` rows at the DB + ORM level; all existing
single-membership behavior is preserved.

**Feature flag**: none — see **Guiding Decisions** (pre-production, no flag).

Changes:
1. [organizations/models.py](../organizations/models.py): `user` OneToOne→FK,
   `related_name="organization_memberships"`, add `UniqueConstraint(user, organization)`, rewrite
   the hard-gate docstring.
2. Migration: `AlterField` + `AddConstraint` (use the `add-migration` skill; lock-aware — see Risk
   notes).
3. [users/models.py:46-61](../users/models.py#L46-L61) `is_organization_admin(organization)`:
   resolve against the **named** org —
   `self.organization_memberships.filter(organization_id=..., is_active=True, role=ADMIN).exists()`
   — instead of the single reverse accessor.
4. [organizations/services.py](../organizations/services.py): the two `hasattr(user, "organization_membership")`
   guards ([:286](../organizations/services.py#L286), [:342](../organizations/services.py#L342))
   become "single active membership" reads via `get_active_organization_membership` (semantics
   unchanged this phase — still refuses a 2nd membership; relaxed in Phase 4/Phase 6).
5. `get_active_organization_membership` ([organizations/models.py:18-38](../organizations/models.py#L18-L38)):
   temporary resolution = the user's single active membership via
   `user.organization_memberships.filter(is_active=True).first()` (header resolver arrives Phase 2a).
6. Sweep every direct `user.organization_membership` / `.organization_membership` reverse read in
   non-test app code and the factories ([organizations/tests](../organizations/tests), factories)
   to the new manager name.

Use-case: shared scaffolding — enables Use-cases 1–7.

Tests:
- **Unit**: `organizations/tests/test_models.py` — a user can hold 2 memberships in different orgs;
  `unique(user, organization)` rejects a duplicate in the same org; `is_organization_admin` is
  per-org.
- **Integration**: `organizations/tests/test_views.py` — full existing suite green unchanged
  (single-membership users see identical responses); `current` still 200/404 as before.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Schema change plus
a cross-app sweep of reverse-accessor reads with subtle invariant edits.

**Reusable skills**: `add-migration` (the `AlterField` + `AddConstraint`); `write-tests`.

Acceptance: after migrate, `OrganizationMembership.objects.create` succeeds for a user who already
has a membership in a *different* org and is rejected for the *same* org; the existing test suite is
green with no behavior change for single-membership users.

---

### Phase 2a — Active-org resolver + header happy path

**Goal**: when `X-Organization-Id` names an org the caller is an active member of, that org is the
request's active org; absent header keeps today's single-membership behavior.

**Feature flag**: none.

Changes:
1. New `TenantScopedViewMixin.initial()` in [common/utils/view_utils.py](../common/utils/view_utils.py):
   after `super().initial()` (so DRF auth has populated `request.user`), call a resolver that reads
   the header, looks up `request.user.organization_memberships.filter(is_active=True, ...)`, and
   stashes the result on `request.organization`, `request.organization_membership`, and
   `request.user._active_membership`. Add the mixin to the shared base viewsets
   (`VintaScheduleModelViewSet`, `ReadOnlyVintaScheduleModelViewSet`, and the `No*`/`*Only` variants).
2. `get_active_organization_membership(user)` reads `user._active_membership` when set; falls back
   to the single-membership query otherwise (off-request callers).
3. This phase resolves only the **happy/implicit** rows of the table in **API Design** — header
   matches a membership → use it; header absent + single membership → use it. Non-member and
   multi-org-no-header rows are stubbed to the pre-existing behavior (no 400/403 yet) and land in
   2b/2c.

Use-case: **Use-case 2** — active org selected via header.

Tests:
- **Integration**: `organizations/tests/test_org_resolution.py` — header naming the caller's org
  resolves that org's queryset; absent header with one membership behaves as before; the resolved
  membership is what `get_active_organization_membership` returns inside a view.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. DRF lifecycle wiring across multiple base
classes + the stash/read seam.

**Reusable skills**: `write-tests`.

Acceptance: a user with memberships in orgs A and B receives A's data when sending
`X-Organization-Id: <A>` and B's data when sending `<B>`, through any internal REST viewset.

---

### Phase 2b — Reject multi-org requests with no header (400)

**Goal**: a user with 2+ active memberships who omits the header gets a clear 400 instead of an
ambiguous/implicit org.

**Feature flag**: none.

Changes:
1. In the resolver (Phase 2a): when active-membership count ≥ 2 and the header is absent, raise
   `ValidationError` → **400** `{"detail": "X-Organization-Id header required."}`.
2. Ensure onboarding/gated paths (0 memberships) and the new `GET /organizations/mine/` are exempt
   (the latter must function precisely *because* the client has no id yet — it never requires the
   header; wired in Phase 3).

Use-case: **Use-case 3** — header-absent multi-org is rejected.

Tests:
- **Integration**: `test_org_resolution.py` — two memberships + no header → 400; one membership +
  no header → still resolves (no regression); zero memberships → still gated, not 400.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini`. One branch on the resolver + tests.

**Reusable skills**: `write-tests`.

Acceptance: a 2-org user calling any tenant-scoped endpoint without `X-Organization-Id` gets 400; a
1-org user is unaffected.

---

### Phase 2c — Reject non-member org header (403)

**Goal**: a header naming an org the caller is not an active member of is refused, never silently
ignored.

**Feature flag**: none.

Changes:
1. In the resolver: header present but no matching active membership → raise `PermissionDenied` →
   **403**. Covers both "org exists but user isn't a member" and "inactive membership in that org".

Use-case: **Use-case 4** — invalid/non-member org header.

Tests:
- **Integration**: `test_org_resolution.py` — header for a non-member org → 403; header for an org
  where the membership is `is_active=False` → 403; header for a member org → 200.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini`. One branch + tests.

**Reusable skills**: `write-tests`.

Acceptance: sending `X-Organization-Id` for an org the user doesn't actively belong to returns 403
on every internal REST endpoint.

---

### Phase 3 — List my organizations endpoint

**Goal**: the frontend can fetch the caller's active memberships to populate an org switcher.

**Feature flag**: none — purely additive new surface.

Changes:
1. [organizations/views.py](../organizations/views.py) `OrganizationViewSet`: add
   `@action(detail=False, methods=["get"], url_path="mine", permission_classes=[IsAuthenticated])`
   returning the caller's active memberships. Exempt from the header requirement (Phase 2b) — it is
   the discovery endpoint.
2. [organizations/serializers.py](../organizations/serializers.py): a `MyMembershipSerializer`
   (nested `{organization: {id, name}, role}`), or reuse `CurrentMembershipSerializer` if its shape
   already fits.

Use-case: **Use-case 5** — list a user's organizations.

Tests:
- **Integration**: `organizations/tests/test_views.py` — multi-org user gets all active memberships;
  inactive memberships are excluded; gated user gets `200 []`; no `X-Organization-Id` needed.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini`. Read-only action mirroring
`current`.

**Reusable skills**: `create-rest-endpoint`; `write-tests`.

Acceptance: `GET /organizations/mine/` returns the authenticated caller's active memberships
(org id, name, role) without requiring the active-org header.

---

### Phase 4 — Multi-org invitation accept

**Goal**: a user already in one org can accept an invitation into another org, gaining a second
membership.

**Feature flag**: none.

Changes:
1. [organizations/services.py](../organizations/services.py) `accept_invitation`
   ([:270-307](../organizations/services.py#L270-L307)) and `provision_tenant_for_user`
   ([:309-376](../organizations/services.py#L309-L376)): drop the blanket
   `hasattr(user, "organization_membership")` refusal; refuse only when an active membership in the
   **invitation's org** already exists. The `unique(user, organization)` constraint (Phase 1) is the
   backstop against a duplicate.
2. [organizations/views.py](../organizations/views.py) `AcceptInvitationView`
   ([:646-689](../organizations/views.py#L646-L689)): `UserAlreadyHasMembershipError` now means
   "already a member of *this* org" → keep the 400, but it's the same-org case only.
3. Reconcile `OrganizationInvitation.email = EmailField(unique=True)`
   ([organizations/models.py:195](../organizations/models.py#L195)) with multi-org invites — a user
   may legitimately receive invitations from several orgs. Verify whether this unique blocks
   concurrent pending invites; if so, relax to `unique(email, organization)` in this phase's
   migration. (Flagged in **Open Questions** if behavior is intended.)

Use-case: **Use-case 6** — accept an invitation while already in another org.

Tests:
- **Integration**: `organizations/tests/test_views.py` /
  `accounts/tests/test_social_gated_onboarding.py` — user in org A accepts an invite to org B → two
  active memberships; accepting a second invite into org A (already a member) → 400; the new
  membership is reachable via the header (`GET /organizations/mine/` shows both).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Touches the provisioning invariant across
service + view + (possibly) an invitation uniqueness migration.

**Reusable skills**: `add-migration` (only if the invitation unique constraint changes);
`write-tests`.

Acceptance: a user with an existing membership successfully accepts an invitation into a different
org and ends with two active memberships; a duplicate accept into an org they already belong to
returns 400.

---

### Phase 5 — Allow creating an additional organization

**Goal**: an authenticated user who already belongs to one or more orgs can create another and
become its admin.

**Feature flag**: none.

Changes:
1. [organizations/permissions.py](../organizations/permissions.py) `OrganizationManagementPermission.has_permission`
   ([:18-31](../organizations/permissions.py#L18-L31)): stop gating `create` to membership-less
   users. Allow any authenticated user to `POST /organizations/`. Keep `has_object_permission`
   org-scoped so they can only act on orgs they belong to.
2. Confirm `OrganizationService.create_organization`
   ([organizations/services.py:58-95](../organizations/services.py#L58-L95)) makes the creator
   ADMIN of the new org via a fresh membership — already does; just verify it no longer collides
   with the (now removed) single-membership gate.

Use-case: **Use-case 7** — create an additional org as an existing member.

Tests:
- **Integration**: `organizations/tests/test_views.py` — a user with a membership in org A creates
  org C → becomes ADMIN of C, now has two memberships; `GET /organizations/mine/` lists both;
  resolving `X-Organization-Id: <C>` scopes to C.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini`. Single permission relaxation +
tests.

**Reusable skills**: `write-tests`.

Acceptance: `POST /organizations/` succeeds for a user who already has memberships, creating a new
org with the caller as its admin.

### Phase 6 — Document the `X-Organization-Id` header in OpenAPI

**Goal**: every tenant-scoped REST operation that honors `X-Organization-Id` declares it as an
OpenAPI header parameter, so generated clients / Swagger UI surface the header instead of leaving
the tenant-selection contract invisible. (Added 2026-06-14 — the resolver from Phase 2a reads the
header in `TenantScopedViewMixin.initial()`, which drf-spectacular's per-operation introspection
never sees, so the header was absent from `schema.yml`.)

**Feature flag**: none — additive doc-only surface, no runtime behavior change.

Changes:
1. New `common/openapi.py`: `TenantScopedAutoSchema(drf_spectacular.openapi.AutoSchema)` overriding
   `get_override_parameters()`. When `isinstance(self.view, TenantScopedViewMixin)` AND the current
   action is NOT in the view's `active_org_optional_actions` (and the view isn't fully opted out via
   `active_org_resolution_optional`), append
   `OpenApiParameter(name="X-Organization-Id", type=OpenApiTypes.STR, location=OpenApiParameter.HEADER, required=False, description=<the resolution contract: required when the caller has 2+ active memberships; 400 if omitted by a multi-org caller, 403 if it names a non-member org>)`.
   `required=False` because single-membership callers may omit it. Call `super().get_override_parameters()`
   and concatenate so existing per-view parameters are preserved.
2. `vinta_schedule_api/settings/base.py`: change `REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"]` from
   `"drf_spectacular.openapi.AutoSchema"` to the new `"common.openapi.TenantScopedAutoSchema"`.
3. Regenerate `schema.yml` (`uv run python manage.py spectacular --color --file schema.yml`).

This is the centralized mechanism (one class, auto-covers every current + future
`TenantScopedViewMixin` route) — a raw `POSTPROCESSING_HOOKS` callable can't honor the
"view is a `TenantScopedViewMixin` subclass + skip the opt-out actions" rule because the assembled
result dict no longer carries the view class.

Spec use-case: shared API-contract documentation — supports Use-cases 2–4 (the header contract).

Tests:
- **Unit** (`common/tests/test_openapi.py` or `organizations/tests/`): generate the schema in-process
  (drf-spectacular `SchemaGenerator().get_schema(request=None, public=True)`), assert a representative
  tenant-scoped operation (e.g. `GET /calendars/`) declares an `X-Organization-Id` header parameter
  with `required: false`, and assert an opted-out operation (`GET /organizations/mine/`,
  `POST /organizations/`) and a non-tenant operation do NOT declare it.
- **Schema gate**: `schema.yml` regenerated; the `backend-schema-local` pre-commit hook passes (no
  drift).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single small
schema class + a settings line + a generation test; established drf-spectacular extension point.

**Reusable skills**: `write-tests`.

Acceptance: `schema.yml` declares `X-Organization-Id` as a non-required header parameter on every
tenant-scoped REST operation and omits it from the `mine`/`create` opt-out actions and non-tenant
operations; the schema pre-commit hook is green.

## 6. Risk & Rollout Notes

- **No feature flag** — justified in **Guiding Decisions** (pre-production, 1:1 migration,
  single-membership behavior preserved). If a tenant is onboarded before this ships, revisit and add
  a per-request flag around the resolver.
- **Phase 1 migration locks**: converting the OneToOne to FK drops the implicit
  `UNIQUE(user_id)` index and adds a composite unique. On a small/empty table this is trivial; on a
  populated table the `AddConstraint` takes a brief `ACCESS EXCLUSIVE` lock to build the unique
  index. Since there is no production data, run inline. The `add-migration` skill / `migration-author`
  agent should still author the **reverse** path (FK→OneToOne, drop composite, restore single
  unique) — reversible.
- **Resolver placement**: the mixin must call the resolver **after** `super().initial()` so DRF
  authentication has set `request.user` (JWT user is not available at Django-middleware time). Any
  internal view *not* built on the shared base viewsets (`AcceptInvitationView`,
  `generics.*`) won't get the stash — those are onboarding endpoints that intentionally don't need a
  resolved active org; the helper's off-request fallback covers them.
- **400/401/403 ordering**: the resolver runs in `initial()` alongside `check_permissions`. Ensure
  unauthenticated requests still 401 first (auth runs before the resolver), not 400/403 from the
  resolver.
- **Rollback**: each phase is independently revertible. Reverting Phase 2a–2c restores
  single-membership implicit resolution. Reverting Phase 1 (the migration) is the only schema
  rollback and is authored reversible.
- **No backfill** — existing membership rows are valid as-is under the new FK.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should header-absent resolution eventually use a **stored "last-used org"** instead of 400 for multi-org users? | Ship 400 now; add a `last_used` / `is_default` flag on `OrganizationMembership` later if UX demands a no-header default. | Product |
| `OrganizationInvitation.email` is globally `unique=True`. Does multi-org inv(same email invited by two orgs concurrently) require `unique(email, organization)`? | Relax to `unique(email, organization)` in Phase 4 **if** tests show the global unique blocks legitimate concurrent invites; otherwise leave as-is. | Eng |
| Should `GET /organizations/current/` be **deprecated** in favor of `mine/` + the header, or kept? | Keep both — `current` reflects the resolved active org; `mine` lists choices. | Eng |
| Admin endpoints (`organization-members`, `service-accounts`) — confirm they scope to the **resolved** org for a multi-org admin (they read `get_active_organization_membership`, so they will once Phase 2a lands). | Covered by the resolver; add an explicit multi-org admin test in Phase 2a. | Eng |

## 8. Touch List

**Phase 1 — Membership FK migration**
- edit [organizations/models.py](../organizations/models.py) — `user` FK, `related_name`, unique constraint, docstring
- new `@organizations/migrations/00XX_membership_user_fk.py`
- edit [users/models.py](../users/models.py) — `is_organization_admin` per-org
- edit [organizations/services.py](../organizations/services.py) — provisioning guard reads
- edit [organizations/models.py](../organizations/models.py) `get_active_organization_membership` — single-membership fallback
- edit factories + any non-test reverse-accessor reads surfaced by `grep -rn "organization_membership\b"`

**Phase 2a — Resolver + happy path**
- edit [common/utils/view_utils.py](../common/utils/view_utils.py) — `TenantScopedViewMixin.initial()` + add to base viewsets
- edit [organizations/models.py](../organizations/models.py) `get_active_organization_membership` — read `_active_membership`
- new `organizations/tests/test_org_resolution.py`

**Phase 2b — No-header 400**
- edit resolver in [common/utils/view_utils.py](../common/utils/view_utils.py)
- edit `organizations/tests/test_org_resolution.py`

**Phase 2c — Non-member 403**
- edit resolver in [common/utils/view_utils.py](../common/utils/view_utils.py)
- edit `organizations/tests/test_org_resolution.py`

**Phase 3 — List my orgs**
- edit [organizations/views.py](../organizations/views.py) — `mine` action
- edit [organizations/serializers.py](../organizations/serializers.py) — membership-list serializer
- edit [organizations/tests/test_views.py](../organizations/tests/test_views.py)

**Phase 4 — Multi-org invite accept**
- edit [organizations/services.py](../organizations/services.py) — `accept_invitation`, `provision_tenant_for_user`
- edit [organizations/views.py](../organizations/views.py) — `AcceptInvitationView` error semantics
- new `@organizations/migrations/00XX_invitation_email_org_unique.py` (only if uniqueness relaxed)
- edit [organizations/tests/test_views.py](../organizations/tests/test_views.py), [accounts/tests/test_social_gated_onboarding.py](../accounts/tests/test_social_gated_onboarding.py)

**Phase 5 — Create additional org**
- edit [organizations/permissions.py](../organizations/permissions.py) — relax `OrganizationManagementPermission.has_permission`
- edit [organizations/views.py](../organizations/views.py) — `create` opt-out + `OrganizationViewSet.create` override
- edit [organizations/tests/test_views.py](../organizations/tests/test_views.py)

**Phase 6 — Document `X-Organization-Id` in OpenAPI**
- new `@common/openapi.py` — `TenantScopedAutoSchema`
- edit [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py) — `REST_FRAMEWORK["DEFAULT_SCHEMA_CLASS"]`
- edit `schema.yml` (regenerated)
- new `common/tests/test_openapi.py`

## 9. Amendments

- **2026-06-14** — Appended **Phase 6 — Document `X-Organization-Id` in OpenAPI**. The Phase 2a resolver reads the header in `TenantScopedViewMixin.initial()`, which drf-spectacular never introspects, so `schema.yml` did not declare the header on any route. Phase 6 adds a shared `TenantScopedAutoSchema` that injects the header parameter on tenant-scoped operations (skipping `active_org_optional_actions`). Append-only: no existing phase rewritten, no branch force-pushed; implemented forward as `plan/multi-org-membership/phase-6` stacked on phase-5.
- edit [organizations/tests/test_views.py](../organizations/tests/test_views.py), [organizations/tests/test_permissions.py](../organizations/tests/test_permissions.py)
