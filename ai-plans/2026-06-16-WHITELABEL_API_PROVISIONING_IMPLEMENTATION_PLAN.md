# White-Label API-Only Provisioning — Implementation Plan

> No `..._SPEC.md` sibling exists. The decisions below were captured through a Step 0
> interrogation (recorded in **Guiding Decisions**) and stand in for the spec. Use-cases
> referenced by each phase are the numbered entries under **Use-cases** in this document.
>
> **Cross-repo plan.** Backend work lives in this repo (`vinta-schedule`). Frontend work
> (`Phase 10b`, `Phase 11b`) lives in `vinta-schedule-frontend-web` and runs in a parallel lane.
> File `@paths` are relative to each phase's repo root.
>
> Builds on two landed plans — do not re-derive their work:
> [2026-06-04-AUTH_TENANT_PROVISIONING_IMPLEMENTATION_PLAN.md](2026-06-04-AUTH_TENANT_PROVISIONING_IMPLEMENTATION_PLAN.md)
> (gives us `OrganizationService.provision_tenant_for_user`, `OrganizationInvitation`,
> social-adapter invite auto-join) and
> [2026-06-13-MULTI_ORG_MEMBERSHIP_IMPLEMENTATION_PLAN.md](2026-06-13-MULTI_ORG_MEMBERSHIP_IMPLEMENTATION_PLAN.md)
> (membership is already a ForeignKey; a user can hold many memberships).

## 1. Goals

1. A **Vinta admin** decides, **in the database only**, whether a given `Organization` may invite/create
   other organizations — a single boolean on the `Organization` model, **default disabled**, never
   editable through any GraphQL/REST/serializer surface.
2. An organization with that flag enabled (a **"reseller" org**) gains a fixed **capability bundle**,
   each exercised through the public GraphQL API:
   - **Branding**: customize the authentication pages + transactional emails its child orgs' users see.
   - **Self-managed invitations**: suppress vinta's invitation email and receive the raw invitation
     token/link directly, to render and manage invites inside its own UI.
   - **Token delegation**: mint Public API tokens that themselves carry the org-invite capability.
   - **Child analytics**: list its child organizations with aggregated counts (memberships, calendars,
     events, calendar groups).
3. Organizations **created by a reseller org inherit that reseller's branding**, so every page and email
   their end-users touch reads as the reseller's product — vinta-schedule becomes invisible.
4. None of the bundle is reachable unless the DB flag is enabled: with the flag off, an org behaves
   **byte-for-byte as today** (the backwards-compatibility guarantee).

**Non-goals:**
- **A reseller granting reseller powers to its children.** Only a Vinta admin flips the DB flag; a
  reseller cannot make its child orgs into sub-resellers (the flag is DB-only and never delegated).
- **Federated / token-exchange SSO** (clinic-as-IdP asserting identity). We use the themed OAuth
  redirect; no `id_token`/`access_token` exchange endpoint in v1.
- **A full white-label hosted app.** The tenant app shell / dashboard is not themable; only the
  unavoidable OAuth interstitials and transactional emails carry reseller branding. The reseller
  builds 100% of the end-user UI itself.
- **Per-tenant custom / vanity domains.** Themed pages stay on vinta's domain in v1.
- **Billing / tier self-service** through the API. Tier stays Vinta-ops-controlled.
- **REST** provisioning surface. Provisioning is GraphQL-only (see Guiding Decisions).
- **Real-time / historical analytics.** Child analytics are point-in-time aggregate counts, not a
  time-series or dashboard product.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **The capability switch** | A single boolean **`can_invite_organizations`** on `@organizations/models.py::Organization`, `default=False`. **DB/Django-admin only** — no GraphQL input type, serializer, or REST endpoint reads or writes it. A Vinta admin sets it deliberately. It is the *necessary* gate for the entire capability bundle: with it off, none of the bundle's mutations/queries are reachable regardless of token scopes. |
| **Why one flag unlocks a bundle, not per-capability flags** | The requester defined a single operator decision ("this org may invite others") that confers a fixed set of powers. One column keeps the operator UX trivial (flip one switch) and makes the security boundary auditable (one gate to grep). Fine-grained control within the bundle is still enforced by per-operation `ResourceAccess` scopes on the org's tokens (defense in depth). |
| **Gate is necessary, scope is also necessary** | Every bundle operation checks **both** `acting_org.can_invite_organizations is True` **and** the token's `OrganizationResourceAccess` scope for that operation. The DB flag is the operator's switch; the scope is the reseller's own least-privilege control over its tokens. |
| **Reseller / child modeling** | Reuse `Organization` with a nullable self-referential **`parent`** FK. A reseller is simply an `Organization` with `can_invite_organizations = True`; orgs it creates get `parent = reseller`. No new top-level entity. |
| **Branding inheritance** | `OrganizationBranding` is a one-to-one on the **reseller** org. Resolution walks up `parent` to the **nearest ancestor with `can_invite_organizations = True`** (the reseller) and uses its branding; no such ancestor ⇒ vinta default. So every child of a reseller renders the reseller's pages/emails. |
| **Provisioning transport** | Extend the existing **public GraphQL** API (Strawberry) in `@public_api/`, authenticated by the existing `SystemUser` bearer token, scoped by `ResourceAccess`. Rationale: the reseller is a machine integration; the GraphQL data-plane + token + scope model already exists ([public_api/permissions.py](../public_api/permissions.py), [public_api/middlewares.py](../public_api/middlewares.py)). A parallel REST surface would duplicate auth/scoping/isolation GraphQL already enforces. |
| **Bootstrap (chicken-and-egg)** | Vinta ops creates the first reseller `Organization`, flips `can_invite_organizations = True` in the DB, and issues it one `SystemUser` token (org-pinned) with the bundle scopes. From there the reseller self-serves: create child orgs, provision users/invitations, mint further per-tenant tokens — all via GraphQL. |
| **Token delegation, not flag delegation** | A reseller may mint Public API tokens carrying the `ORGANIZATION` scope (Capability "token delegation"), so those tokens can also create child orgs **on the reseller's behalf** — but the child orgs they create are still plain orgs (`can_invite_organizations = False`). The reseller can never confer the DB flag. |
| **Self-managed invitations** | `createInvitation` takes `sendEmail: Boolean = true`. When `false` (gated by the flag), vinta sends no email and the mutation **returns the raw invitation token + a constructed invite URL once**, so the reseller renders the link in its own UI. The accept path is unchanged (`OrganizationService.accept_invitation(token, user)`); first themed social login still auto-joins. |
| **Scope additions** | Add `MEMBERSHIP`, `INVITATION`, `BRANDING`, `CHILD_ORG_ANALYTICS` to `PublicAPIResources` ([public_api/constants.py](../public_api/constants.py)); `ORGANIZATION`, `USER`, `SYSTEM_USER` already exist. |
| **Child analytics shape** | A `childOrganizations` query returns each child + point-in-time counts (`membershipCount`, `calendarCount`, `eventCount`, `calendarGroupCount`) via ORM annotations (subquery counts). If annotation perf degrades at scale, swap to a `vw_organization_child_metrics` Postgres view (noted, not built in v1). |
| **User account semantics** | Passwordless / social-only: `createUser` makes a `User` + `Profile` with `set_unusable_password()`, email unverified. The user becomes usable only after the themed Google login (Google verifies the email) and the existing social-adapter invite auto-join. Vinta's email-verification posture is preserved, not bypassed. |
| **No rollout feature flag** | This repo has **no flag framework** and is pre-production. The `can_invite_organizations` column is a per-org **capability** switch, not an A/B rollout flag, and it defaults off, so every existing org is unchanged. Each behavior-touching change is additionally null-guarded (no branding row ⇒ vinta default). A flag-removal phase is therefore N/A. |
| **Tenant isolation** | Reuses `OrganizationResourceAccess` + `SystemUser.organization` / `X-Public-Api-Organization-Id` unchanged. The acting org is the token's org (or the validated header). Every bundle mutation validates the target org is the acting org or a descendant of it. |

## 3. Data Model Changes

### 3.1 `Organization` — `parent` FK + DB-only capability flag

In @organizations/models.py (`Organization`, near line 74):

```python
parent = models.ForeignKey(
    "self",
    null=True,
    blank=True,
    on_delete=models.PROTECT,        # a reseller with live children cannot be deleted out from under them
    related_name="child_organizations",
)

can_invite_organizations = models.BooleanField(default=False)
# Vinta-admin / DB only. NEVER exposed by any GraphQL input, serializer, or REST endpoint.
# Enables the whole reseller capability bundle for this org. See Guiding Decisions.
```

Helpers:

```python
def is_reseller(self) -> bool:
    return self.can_invite_organizations

def get_branding_root(self) -> "Organization | None":
    org = self
    while org is not None:
        if org.can_invite_organizations:
            return org
        org = org.parent
    return None                       # None ⇒ vinta default branding
```

Migration: two additive columns (nullable `parent` FK + boolean default `False`), no backfill. No lock concern.

### 3.2 New `OrganizationBranding`

New model (own file @organizations/models/branding.py, exported from `organizations/models/__init__.py`):

```python
class OrganizationBranding(models.Model):
    organization = models.OneToOneField(           # expected to point at a reseller org
        "organizations.Organization", on_delete=models.CASCADE, related_name="branding",
    )
    app_name = models.CharField(max_length=120)
    logo_url = models.URLField(blank=True, default="")
    primary_color = models.CharField(max_length=9, blank=True, default="")     # #RRGGBB[AA]
    secondary_color = models.CharField(max_length=9, blank=True, default="")
    support_email = models.EmailField(blank=True, default="")                  # From/reply-to for branded mail
    return_url_allowlist = ArrayField(models.URLField(), default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
```

`resolve_branding(org) -> OrganizationBranding | None` = `getattr(org.get_branding_root(), "branding", None)`.

### 3.3 `PublicAPIResources` additions

In @public_api/constants.py — add `MEMBERSHIP`, `INVITATION`, `BRANDING`, `CHILD_ORG_ANALYTICS`. `ORGANIZATION`, `USER`, `SYSTEM_USER` already present.

### 3.4 Shared gate helper

`@public_api/capabilities.py` (new): `assert_org_can_invite(acting_org)` raises a permission error unless `acting_org.can_invite_organizations`. Every bundle resolver calls it after the `ResourceAccess` check. One chokepoint for the gate.

## 4. API Design

All mutations/queries are added to the public GraphQL schema ([public_api/mutations.py](../public_api/mutations.py),
[public_api/queries.py](../public_api/queries.py)), enforced by `IsAuthenticated` + `OrganizationResourceAccess`
with the resource named per row, **and** `assert_org_can_invite(acting_org)` for every bundle operation.
The acting org is the token's org (or the validated `X-Public-Api-Organization-Id`).

### 4.1 `createOrganization` — provision a child org (resource: `ORGANIZATION`, gated)

- Input: `{ name: String! }` (child's `parent` = acting reseller).
- Effect: `Organization` with `parent = acting_org`, `can_invite_organizations = False`. No membership created.
- Returns: `{ organization { id name } }`.

### 4.2 `createUser` — passwordless end-user (resource: `USER`, gated)

- Input: `{ email: String!, firstName: String, lastName: String }`. Idempotent on email.
- Effect: `User` + `Profile`, `set_unusable_password()`, email unverified.

### 4.3 `createInvitation` — link user → child org (resource: `INVITATION` + `MEMBERSHIP`, gated)

- Input: `{ userEmail: String!, organizationId: ID!, role: OrgRole!, sendEmail: Boolean = true }`
  (`organizationId` must be the acting org or a descendant).
- Effect: creates a pending `OrganizationInvitation`. If `sendEmail` ⇒ sends the **reseller-branded** email.
  If `!sendEmail` ⇒ no email; returns the **raw token + invite URL once** (self-managed invitations).
- Returns: `{ invitation { id email expiresAt }, token: String, inviteUrl: String }` (token/url null when emailed).
- Errors: user already a member of that org → typed `UserAlreadyHasMembershipError`.

### 4.4 `createSystemUserToken` — mint a delegated token (resource: `SYSTEM_USER`, gated)

- Input: `{ organizationId: ID!, integrationName: String!, resources: [String!]! }`
  (`organizationId` = acting org or descendant; `resources` may include `ORGANIZATION` to delegate the
  invite-orgs capability for the reseller's own tokens).
- Effect: `PublicAPIAuthService.create_system_user(integration_name, organization=target)` + `ResourceAccess`.
  Minted tokens still operate only within the reseller's subtree; they cannot set `can_invite_organizations`.
- Returns: `{ systemUserId: ID!, token: String! }` (plaintext once).

### 4.5 `updateBranding` — reseller branding (resource: `BRANDING`, gated)

- Input: `{ appName, logoUrl, primaryColor, secondaryColor, supportEmail, returnUrlAllowlist }` — always
  upserts on the **acting** org (must itself be a reseller; cannot brand someone else's tree).
- Returns: the resolved branding.

### 4.6 `brandingForTenant` — public read for interstitials (query, **unauthenticated**)

- Input: `{ tenantId: ID! }`.
- Effect: returns parent-walked (`get_branding_root`) branding for `tenantId`, or the vinta-default
  sentinel when none. No secrets, no allowlist, rate-limited.
- Returns: `{ appName, logoUrl, primaryColor, secondaryColor }`.

### 4.7 `childOrganizations` — reseller analytics (query, resource: `CHILD_ORG_ANALYTICS`, gated)

- Input: `{ first, after }` (pagination).
- Effect: lists the acting reseller's children with point-in-time aggregate counts.
- Returns: `[{ id, name, createdAt, membershipCount, calendarCount, eventCount, calendarGroupCount }]`.

## 5. Phased Rollout

Ordering: the **GraphQL provisioning contract is the slowest-moving dependency** (the reseller integrates
against it), so it lands first (`Phase 0` → `Phase 5`). Branding storage + read (`Phase 6`–`Phase 8`)
unblock the frontend lanes. `Phase 9` (analytics) is independent of the frontend. `Phase 10a` (first-party
REST branding endpoints, backend, depends on `Phase 6`) unblocks the branding console; the two frontend lanes
(`Phase 10b` → depends on `Phase 7`; `Phase 11b` → depends on `Phase 10a`) run in parallel once their backend
dependencies are merged.

### Phase 0 — Org hierarchy, capability flag, gate helper (foundation)

**Goal**: Ship value: none on its own — scaffolds the `parent` FK, the DB-only `can_invite_organizations`
flag, the scope enum, and the single gate helper every bundle phase consumes.

**Feature flag**: none — pure additive scaffolding; the new boolean defaults off (no reachable behavior change).

Changes:
1. @organizations/models.py: `parent` self-FK, `can_invite_organizations` boolean, `is_reseller()`, `get_branding_root()`.
2. @public_api/constants.py: add `MEMBERSHIP`, `INVITATION`, `BRANDING`, `CHILD_ORG_ANALYTICS`.
3. @public_api/capabilities.py (new): `assert_org_can_invite(acting_org)`.
4. Migration (nullable FK + boolean default).
5. @organizations/admin.py: expose `can_invite_organizations` as an editable field — the **only** place it can be toggled.

Spec use-case: Use-case 1 — Vinta admin enables an org to invite others (data + gate scaffolding).

Tests:
- **Unit**: @organizations/tests/test_models.py — `can_invite_organizations` defaults `False`; `get_branding_root()` walks to the nearest reseller ancestor and returns `None` when there is none; existing orgs migrate to `parent = NULL`, flag off.
- **Unit**: @public_api/tests/test_capabilities.py — `assert_org_can_invite` raises for a flag-off org, passes for a flag-on org.
- **Guard**: @public_api/tests/test_schema_surface.py — GraphQL introspection + a serializer-field scan assert `can_invite_organizations` is absent from every input type and serializer (it is unreachable via any API).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` if the branding-root walk + introspection guard get fiddly) / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-model` (FK + flag migration); `write-tests`.

Acceptance: migration applies; all existing orgs have the flag off and behave as today; `assert_org_can_invite` gates correctly; no API surface exposes the flag.

### Phase 1 — `createOrganization` (gated child provisioning)

**Goal**: A reseller org can create a child organization through GraphQL.

**Feature flag**: none — net-new mutation, gated by the flag + `ORGANIZATION` scope.

Changes:
1. @public_api/mutations.py: `create_organization` (`OrganizationResourceAccess('ORGANIZATION')` + `assert_org_can_invite`); child gets `parent = acting_org`, flag `False`.
2. @public_api/types.py: input/result types (no flag field).

Spec use-case: Use-case 2 — Reseller creates a child org.

Tests:
- **Unit**: @public_api/tests/test_mutations.py — happy path under a reseller; **flag-off acting org → permission error even with the scope**; duplicate name under parent → validation error.
- **Integration**: @public_api/tests/test_provisioning_flow.py — reseller token creates a child with `parent` set and flag off; no-scope token denied; confirm the flag can be flipped only via ORM/admin, never the mutation.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: a reseller creates a child org; a flag-off org (even fully scoped) is refused; children are never resellers.

### Phase 2 — `createUser` (passwordless shell)

**Goal**: A reseller can provision a passwordless end-user by email.

**Feature flag**: none — net-new mutation, gated by the flag + `USER` scope.

Changes:
1. @public_api/mutations.py: `create_user` (gate + scope); `set_unusable_password()`, email unverified, idempotent on email.
2. @public_api/types.py: input/result types.

Spec use-case: Use-case 3 — Reseller provisions a passwordless end-user.

Tests:
- **Unit**: @public_api/tests/test_mutations.py — creates `User` + `Profile`, password unusable, email unverified; repeat email idempotent; flag-off denied.
- **Integration**: @public_api/tests/test_provisioning_flow.py — provisioned user cannot password-login; no-`USER`-scope denied.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: GraphQL yields a membership-less, passwordless, unverified user; idempotent; gated.

### Phase 3 — `createInvitation` (branded-email path)

**Goal**: A reseller can invite a provisioned user into a child org; the user receives the reseller-branded
invitation email and auto-joins on first themed login.

**Feature flag**: none — net-new mutation, gated by the flag + `INVITATION`/`MEMBERSHIP` scopes.

Changes:
1. @public_api/mutations.py: `create_invitation` (default `sendEmail=true`) creating a pending `OrganizationInvitation` in the target child, reusing existing invite machinery; maps `UserAlreadyHasMembershipError` to a GraphQL error; `organizationId` validated as acting-org-or-descendant.
2. @public_api/types.py: input/result types + `OrgRole` enum.

Spec use-case: Use-case 4 — Reseller invites a user (branded-email path).

Tests:
- **Unit**: @public_api/tests/test_mutations.py — creates a pending invite addressed to the email; already-member → typed error; off-subtree `organizationId` → permission error; flag-off denied.
- **Integration**: @public_api/tests/test_provisioning_flow.py — full chain createOrganization → createUser → createInvitation leaves a pending invite; simulated social login auto-joins it (reuses `provision_tenant_for_user`).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Mutation + invitation service + auto-join interplay.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: after the chain a pending invite exists in the child; social login by that email yields an active membership in the child, no stray org.

### Phase 4 — Self-managed invitations (email suppression + token return)

**Goal**: A reseller can suppress vinta's invitation email and get the raw token + invite URL to render the
invite link inside its own UI.

**Feature flag**: none — extends `createInvitation`; the `sendEmail=false` branch is gated by the flag.

Changes:
1. @public_api/mutations.py: honor `sendEmail=false` — skip the email send, surface the raw token + a `inviteUrl` built from the reseller's `return_url_allowlist`/configured invite-URL template; token returned **once**.
2. @public_api/services.py: ensure the invite-creation path can return the pre-hash raw token (mirror the `create_system_user` once-returned-token pattern) without persisting plaintext.

Spec use-case: Use-case 5 — Reseller self-manages invitations (Capability: self-managed invitations).

Tests:
- **Unit**: @public_api/tests/test_mutations.py — `sendEmail=false` sends no email and returns a non-null token + inviteUrl; the returned token validates through `accept_invitation`; `sendEmail=true` returns null token (unchanged from Phase 3); flag-off denied.
- **Integration**: @public_api/tests/test_provisioning_flow.py — a token obtained via `sendEmail=false` drives a successful accept/auto-join; no plaintext token is stored in the DB.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches token hashing + accept path; security-sensitive.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: `sendEmail=false` yields a working raw token + URL with no email sent and no plaintext persisted; `sendEmail=true` behavior is unchanged.

### Phase 5 — `createSystemUserToken` (token delegation)

**Goal**: A reseller can mint Public API tokens — optionally carrying the `ORGANIZATION` scope so those tokens
can also create child orgs on the reseller's behalf — and store them per integration/tenant.

**Feature flag**: none — net-new mutation, gated by the flag + `SYSTEM_USER` scope.

Changes:
1. @public_api/mutations.py: `create_system_user_token` calling `PublicAPIAuthService.create_system_user(integration_name, organization=target)` + requested `ResourceAccess`; `organizationId` validated as acting-org-or-descendant; plaintext token returned once.
2. @public_api/types.py: input/result types.

Spec use-case: Use-case 6 — Reseller mints delegated tokens (Capability: token delegation).

Tests:
- **Unit**: @public_api/tests/test_mutations.py — returns `{id}:{token}` once; `SystemUser.organization = target`; requested resources attached; minted token **cannot** set `can_invite_organizations`; off-subtree target rejected; flag-off denied.
- **Integration**: @public_api/tests/test_provisioning_flow.py — a minted token bearing `ORGANIZATION` can create a child under the reseller subtree but cannot reach another reseller's tree; a minted token without the flag-on org context cannot self-grant the capability.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Token minting + scope grant + isolation + the no-flag-delegation invariant.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: a reseller mints a working token (optionally invite-capable) pinned to its subtree; minted tokens can never confer the DB flag or escape the subtree.

### Phase 6 — Branding storage + `updateBranding`

**Goal**: A reseller can store its branding (logo, colors, app name, support email, return-URL allowlist).

**Feature flag**: none — new model + mutation; orgs without a branding row keep vinta defaults (null-guarded).

Changes:
1. @organizations/models/branding.py (new) + `__init__.py` export + migration.
2. `resolve_branding(org)` helper (reseller-ancestor walk → branding | `None`).
3. @public_api/mutations.py: `update_branding` (gate + `BRANDING` scope) upserting on the acting reseller org.

Spec use-case: Use-case 7 — Reseller customizes branding (Capability: branding).

Tests:
- **Unit**: @organizations/tests/test_branding.py — `resolve_branding` returns the reseller's row for a child, `None` when unset; upsert updates in place.
- **Integration**: @public_api/tests/test_mutations.py — non-reseller acting org → gate error; reseller upserts; missing `BRANDING` scope denied.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to Tier 3 if the ancestor walk gets hairy) / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-model`; `graphql-public-query`; `write-tests`.

Acceptance: a reseller upserts branding on its own org; children resolve to it; unset subtrees resolve to vinta default.

### Phase 7 — `brandingForTenant` public read query

**Goal**: The frontend can fetch resolved, secret-free branding for a tenant **before any session exists**.

**Feature flag**: none — additive read-only query; returns vinta default when unset.

Changes:
1. @public_api/queries.py: `branding_for_tenant(tenant_id)` — **unauthenticated**, rate-limited, parent-walked branding or vinta-default sentinel; never exposes the allowlist or secrets.

Spec use-case: shared read API consumed by Use-case 11 (frontend interstitials).

Tests:
- **Unit**: @public_api/tests/test_queries.py — returns reseller branding for a child; default sentinel for an unbranded subtree; never includes the allowlist/support email-from internals.
- **Integration**: @public_api/tests/test_queries.py — callable without a token; rate-limited; unknown tenant id returns the default (no enumeration oracle).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `graphql-public-query`; `write-tests`.

Acceptance: unauthenticated `brandingForTenant` returns correct branding for a branded subtree and vinta default otherwise, with no secret fields.

### Phase 8 — Reseller-branded transactional emails

**Goal**: The invitation + verification emails a child org's user receives read as the reseller's product.

**Feature flag**: none — templates resolve reseller branding; **no reseller ancestor / no branding row ⇒
today's vinta email, unchanged** (backwards-compat guarantee, asserted by test).

Changes:
1. @accounts/account_adapters.py + @templates/accounts/notifications/emails/* + @templates/organizations/emails/organization_invitation.body.html: inject resolved branding (app name, logo, colors, From/reply-to = `support_email`) via `resolve_branding`.
2. Replace hardcoded "Vinta Schedule" / `vinta_schedule.com.br` strings with branding-context variables that fall back to the vinta defaults when unresolved.

Spec use-case: Use-case 9 — Reseller-branded transactional emails.

Tests:
- **Integration**: @accounts/tests/test_email_branding.py — an invite to a user in a branded subtree renders the reseller's app name/logo and From address; an invite under no reseller renders **today's vinta email byte-for-byte**; no vinta domain leaks in the branded case.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `write-tests`.

Acceptance: branded subtrees' emails show reseller branding and no vinta domain; non-reseller orgs' emails unchanged.

### Phase 9 — `childOrganizations` analytics query

**Goal**: A reseller can list its child orgs with aggregated counts (memberships, calendars, events, calendar groups).

**Feature flag**: none — net-new query, gated by the flag + `CHILD_ORG_ANALYTICS` scope.

Changes:
1. @public_api/queries.py: `child_organizations` resolver (gate + scope) — children of the acting reseller, annotated with subquery counts; paginated.
2. @public_api/types.py: `ChildOrganizationMetrics` type.

Spec use-case: Use-case 10 — Reseller lists child orgs + analytics (Capability: child analytics).

Tests:
- **Unit**: @public_api/tests/test_queries.py — counts are correct per child; only the acting reseller's own children are returned (no cross-reseller leak); flag-off / missing-scope denied.
- **Integration**: @public_api/tests/test_queries.py — a reseller with N children and seeded data gets accurate aggregate counts; pagination stable.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Annotated aggregate query + isolation.

**Reusable skills**: `graphql-public-query`; `write-tests`. (If swapped to a view later: `create-postgres-view`.)

Acceptance: a reseller sees only its children with correct aggregate counts; flag-off / unscoped tokens are denied.

### Phase 10a — First-party REST branding endpoints *(backend — added 2026-06-17 per requester; depends on Phase 6)*

**Goal**: The first-party frontend (the reseller branding console, `Phase 11b`) can read and edit a reseller
org's branding — the values that theme the auth screens (`Phase 10b`) and transactional emails (`Phase 8`) —
through the project's **internal REST** surface (DRF), not the public GraphQL API. Rationale: the public GraphQL
`updateBranding` (`Phase 6`) is the **reseller machine integration's** surface; the first-party frontend follows
the project convention of consuming **REST** (`create-rest-endpoint`) for first-party UIs. Both write the same
`OrganizationBranding` row, so the auth-screen and email consumers are unchanged.

**Feature flag**: none — new REST endpoint; orgs without a branding row keep vinta defaults (null-guarded, same as `Phase 6`).

Changes:
1. @organizations/serializers.py (or a branding-scoped serializer module): `OrganizationBrandingSerializer` exposing
   `app_name, logo_url, primary_color, secondary_color, support_email, return_url_allowlist` (validates color format
   `#RRGGBB[AA]` and that each `return_url_allowlist` entry is a URL). NEVER exposes `can_invite_organizations` or
   `organization` as writable (the org is the acting org).
2. @organizations/views.py: a DRF viewset on the project's `VintaScheduleModelViewSet` base, organization-scoped,
   **reseller-admin-gated** — the acting org must itself be a reseller (`can_invite_organizations is True`, reuse the
   spirit of `assert_org_can_invite`; a non-reseller org gets 403) AND the caller must be an org **admin** (reuse the
   existing admin-role permission used by other admin-only viewsets). Operations: retrieve + upsert (PUT/PATCH) the
   **acting org's own** branding (create-on-first-write). It edits only the acting reseller org's row — it cannot brand
   another org's tree (mirror `Phase 6`'s "upsert on the acting org" rule). A GET with no row yields the vinta-default
   representation or 404 per the project's REST convention for "not yet configured" (pick one, document it).
3. @organizations/routes.py: register the route (e.g. `/branding/` scoped to the acting org). Regenerate `schema.yml`.

Spec use-case: Use-case 12 — Reseller staff manage branding (first-party REST surface consumed by `Phase 11b`).

Tests:
- **Unit/Integration**: @organizations/tests/test_branding_rest.py (new) — a reseller-admin GETs then PUT/PATCH-upserts
  branding on their own org (round-trips the fields); color-format + allowlist-URL validation rejects bad input (400);
  a **non-reseller** org → 403; a **non-admin** member of a reseller → 403; the endpoint edits ONLY the acting org's row
  (cannot target another org); `can_invite_organizations` is absent from the serializer (extend/parallel the `Phase 0`
  guard for the REST surface).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` if the admin+reseller permission
composition or the upsert-on-acting-org logic gets fiddly) / `gpt-5-mini` / `gemini-2.5-flash`.

**Reusable skills**: `create-rest-endpoint`; `write-tests`.

Acceptance: a reseller-admin reads + upserts their org's branding via REST; non-resellers and non-admins are refused;
the row written is the same `OrganizationBranding` the emails (`Phase 8`) and auth screens (`Phase 10b`) resolve; the
DB-only capability flag is not exposed.

### Phase 10b — Frontend: themed OAuth interstitials *(repo: vinta-schedule-frontend-web — parallel lane, depends on Phase 7)*

**Goal**: The loading/success, finish-signup, and error pages a child org's user sees during Google login carry
the reseller's branding and return the user to the reseller's app.

**Feature flag**: none — pages call `brandingForTenant`; a null/default response renders today's vinta pages unchanged.

Changes:
1. @src/lib/branding.ts (new): resolve tenant id from the OAuth flow (state/query param), fetch `brandingForTenant`, expose a `BrandingProvider` with vinta-default fallback.
2. @src/components/layout/auth-navbar.tsx + @src/components/layout/auth-layout.tsx: consume branding instead of the hardcoded `/vinta-wordmark.svg`.
3. @src/app/auth/social/[provider]/success/page.tsx, @src/app/auth/social/finish-signup/page.tsx, @src/app/auth/social/error/page.tsx: themed via the provider.
4. @src/app/auth/social/[provider]/callback/route.tsx: honor a `next`/return-URL param **validated server-side against the tenant's allowlist** (off-allowlist ⇒ default dashboard).

Spec use-case: Use-case 11 — Branded, discreet social-login interstitials.

Tests:
- **Unit**: @src/lib/branding.test.ts — falls back to vinta default on null/error; never throws on a missing tenant id.
- **Component**: themed states of `auth-navbar` / `auth-layout` render the resolved branding (logo/app-name/colors) and the vinta default on a null branding response.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Multi-component theming + return-URL validation.

**Reusable skills** *(frontend repo)*: `new-hook`, `add-storybook-story`.

Acceptance: a branded tenant's social login shows reseller-branded interstitials and redirects only to an allowlisted URL; an unbranded tenant shows today's vinta pages.

### Phase 11b — Frontend: reseller branding console *(repo: vinta-schedule-frontend-web — parallel lane, depends on Phase 10a)*

**Goal**: Reseller staff can define/edit their branding through a gated UI without engineering.

**Feature flag**: none — net-new gated route; invisible to tenant users.

Changes:
1. @src/app/(partner)/branding/page.tsx (new route group): form for app name, logo, colors, support email, return-URL allowlist, with a live interstitial preview.
2. @src/hooks/branding/use-update-branding.ts: wraps the **first-party REST branding endpoint** from `Phase 10a` (the internal frontend uses REST, not the public GraphQL `updateBranding`).
3. Gate the route to reseller-admin accounts (reuses the existing `useRequireRole` pattern; neutral vinta chrome — reseller staff never see the tenant app shell).

Spec use-case: Use-case 12 — Reseller staff manage branding.

Tests:
- **Unit/Component**: @src/components/branding/branding-form.test.tsx — validation (color format, allowlist URLs), submit calls the mutation, preview reflects inputs.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New gated route + form + preview.

**Reusable skills** *(frontend repo)*: `new-page`, `new-composition`, `new-hook`, `add-storybook-story`.

Acceptance: a reseller-admin sets branding in the console and sees it in the live preview + persisted via `updateBranding`; tenant users cannot reach the route.

## 6. Risk & Rollout Notes

- **No rollout flag** (see Guiding Decisions). Safety rests on the `can_invite_organizations` default-off
  capability switch plus null-guards (no branding ⇒ vinta default), each test-asserted. No flag-removal phase.
- **The capability flag is DB-only by design**: `can_invite_organizations` is editable only via DB/Django admin.
  The `Phase 0` introspection guard asserts it is absent from every GraphQL input type + serializer; treat any
  future PR exposing it through an API as a **blocker**.
- **No flag delegation**: a reseller can mint invite-capable tokens (`Phase 5`) but can never set the DB flag,
  so it cannot promote its children into sub-resellers. `Phase 5` tests assert this invariant.
- **Self-managed invitation tokens** (`Phase 4`) are security-sensitive: the raw token is returned once and never
  persisted in plaintext (mirrors the `SystemUser` token pattern). Review the hashing + accept path adversarially.
- **Tenant isolation**: every bundle mutation/query validates the target is the acting org or a descendant;
  `childOrganizations` and minted tokens are confined to the reseller's subtree. Highest-risk area — review the
  subtree-membership guard adversarially.
- **Open-redirect**: the themed return URL is validated server-side against the reseller's allowlist (`Phase 10b`);
  off-allowlist falls back to the default dashboard.
- **Migrations**: two additive, lock-light changes — `Organization.parent` + `can_invite_organizations`
  (`Phase 0`) and `OrganizationBranding` (`Phase 6`). No row rewrites, no hot-table column adds.
  `on_delete=PROTECT` on `parent` makes deleting a reseller with live children fail loudly.
- **Analytics perf**: `childOrganizations` uses annotated subquery counts; if it degrades for resellers with many
  children, swap to a `vw_organization_child_metrics` view (noted in Open Questions).
- **Email branding regression**: `Phase 8` is the one change touching an existing user-facing surface; its test
  pins non-reseller emails to today's output byte-for-byte to catch branding bleed.
- **Rollback**: each phase is independently revertible; mutation/query phases remove net-new surface (no prod
  caller). Reverting `Phase 0`/`Phase 6` drops additive columns/tables, safe while unpopulated. Frontend lanes
  revert to today's hardcoded vinta branding (the null-default path).

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Logo storage — reseller-hosted URL or upload to vinta storage? | URL-only in v1 (`logo_url`); revisit uploads if asked. | Product + FE |
| Reseller-admin account provisioning — how do reseller staff get the `Phase 11b` login? | Vinta ops issues reseller-admin accounts out-of-band, mirroring the flag flip + first token. Self-serve reseller-admin invites = follow-up. | Eng + Ops |
| Default role for `createInvitation` when omitted. | `MEMBER` (admin stays explicit). | Product |
| Invite-URL template for `sendEmail=false` — a fixed reseller-configured base, or fully reseller-constructed from the returned token? | Return the raw token + a default URL built from the first allowlist entry; reseller may ignore it and build its own. | Product + Eng |
| `childOrganizations` counts — annotation now vs `vw_organization_child_metrics` view. | Annotation in v1; view if perf degrades. | Eng |
| `brandingForTenant` throttle scope — per-IP or per-tenant-id. | Per-IP, reusing the existing public throttle. | Eng |
| Token rotation UX for delegated tokens — console or API-only. | API-only in v1 (re-mint + revoke). | Product |

## 8. Touch List

**Phase 0** (backend)
- @organizations/models.py (edit — `parent` FK, `can_invite_organizations`, helpers)
- @organizations/admin.py (edit — expose the flag in admin only)
- @public_api/constants.py (edit — enum additions)
- @public_api/capabilities.py (new — `assert_org_can_invite`)
- new migration under @organizations/migrations/
- @organizations/tests/test_models.py, @public_api/tests/test_capabilities.py, @public_api/tests/test_schema_surface.py

**Phase 1** (backend)
- @public_api/mutations.py (edit — `create_organization`), @public_api/types.py
- @public_api/tests/test_mutations.py, @public_api/tests/test_provisioning_flow.py

**Phase 2** (backend)
- @public_api/mutations.py (edit — `create_user`), @public_api/types.py
- @public_api/tests/test_mutations.py

**Phase 3** (backend)
- @public_api/mutations.py (edit — `create_invitation`), @public_api/types.py
- @public_api/tests/test_mutations.py, @public_api/tests/test_provisioning_flow.py

**Phase 4** (backend)
- @public_api/mutations.py (edit — `sendEmail=false` branch), @public_api/services.py (edit — return raw token once)
- @public_api/tests/test_mutations.py, @public_api/tests/test_provisioning_flow.py

**Phase 5** (backend)
- @public_api/mutations.py (edit — `create_system_user_token`), @public_api/services.py (reuse `create_system_user`), @public_api/types.py
- @public_api/tests/test_mutations.py, @public_api/tests/test_provisioning_flow.py

**Phase 6** (backend)
- @organizations/models/branding.py (new) + `organizations/models/__init__.py` (export)
- new migration under @organizations/migrations/
- @public_api/mutations.py (edit — `update_branding`)
- @organizations/tests/test_branding.py

**Phase 7** (backend)
- @public_api/queries.py (edit — `branding_for_tenant`)
- @public_api/tests/test_queries.py

**Phase 8** (backend)
- @accounts/account_adapters.py (edit — inject branding context)
- @templates/accounts/notifications/emails/* (edit), @templates/organizations/emails/organization_invitation.body.html (edit)
- @accounts/tests/test_email_branding.py (new)

**Phase 9** (backend)
- @public_api/queries.py (edit — `child_organizations`), @public_api/types.py (edit — `ChildOrganizationMetrics`)
- @public_api/tests/test_queries.py

**Phase 10a** (backend)
- @organizations/serializers.py (edit — `OrganizationBrandingSerializer`)
- @organizations/views.py (edit — reseller-admin-gated branding viewset), @organizations/routes.py (edit — register route)
- regenerate @schema.yml
- @organizations/tests/test_branding_rest.py (new)

**Phase 10b** (frontend — `vinta-schedule-frontend-web`)
- @src/lib/branding.ts (new), @src/lib/branding.test.ts (new)
- @src/components/layout/auth-navbar.tsx, @src/components/layout/auth-layout.tsx (edit)
- @src/app/auth/social/[provider]/success/page.tsx, @src/app/auth/social/finish-signup/page.tsx, @src/app/auth/social/error/page.tsx (edit)
- @src/app/auth/social/[provider]/callback/route.tsx (edit — return-URL allowlist)

**Phase 11b** (frontend — `vinta-schedule-frontend-web`)
- @src/app/(partner)/branding/page.tsx (new)
- @src/components/branding/branding-form.tsx (new), @src/components/branding/branding-form.test.tsx (new)
- @src/hooks/branding/use-update-branding.ts (new)
