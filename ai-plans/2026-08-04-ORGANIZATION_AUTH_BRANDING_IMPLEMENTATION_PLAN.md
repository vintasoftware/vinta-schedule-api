# Organization Auth-Area Branding for All Paying Customers — Implementation Plan

Spec: [2026-08-04-ORGANIZATION_AUTH_BRANDING_SPEC.md](2026-08-04-ORGANIZATION_AUTH_BRANDING_SPEC.md).
This plan translates that spec into phases; it does not re-derive requirements. Where a
phase implements a spec use-case, the use-case is named on the phase.

## 1. Goals

1. Any paying organization with no parent can configure its own branding — app name, logo,
   colors, support address, redirect destination — through the dashboard's REST endpoint,
   the public GraphQL mutation, or Django admin, gated on the existing
   `white_label_branding` entitlement rather than on reseller status.
2. An organization that has a parent cannot configure branding through **any** surface, and
   the refusal comes from the backend rather than from a hidden menu item.
3. An organization admin uploads a logo directly to S3 from the dashboard or through the
   public GraphQL API, and it renders on the accept page, the branded login page, and in
   invitation emails read at any later time.
4. Branding resolution treats a parentless organization as its own branding root, so the
   invitation email and the invitation accept page carry that organization's identity.
5. The post-authentication destination is resolved server-side from the organization's
   single configured `redirect_url`, never from a client-supplied callback, and falls back
   to the dashboard when there is none.
6. An organization admin picks a public slug for their organization self-serve, and
   returning users reach a branded login page through an organization-scoped URL keyed on
   it. A slug is required before branding can be configured.

**Non-goals** (from the spec's **Negative scope**, restated so phases can be checked against
them):

- No custom domains for authentication pages.
- No branding of the signed-in application.
- No rebranding of the external calendar consent screen.
- No branding for organizations inside a hierarchy — no per-child override, no
  reseller-granted opt-out.
- No enforcement that lives only in the interface.
- No change to how reseller branding resolves. The redirect unification is the one
  deliberate exception.
- No image processing — no resizing, cropping, format conversion, or thumbnail generation.
  What the organization uploads is what renders.
- No content moderation of uploaded logos.
- No CDN in front of the logo delivery route. Caching headers only.
- No custom From address and no sending-domain verification.
- No branding on the generic login page when no organization is in the URL.
- No client-side work in this repo. The SPA's changes ship from its own repo against the
  handoff document produced in the final phase.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Branding root** | `Organization.get_branding_root()` returns the nearest reseller ancestor if one exists, otherwise `self` when the organization has no parent, otherwise `None`. Today it returns `None` for a parentless non-reseller, which is exactly the line that locks branding to resellers. Changing this one method is what widens the read side; every presentation caller already flows through it. |
| **Write gate** | Writes require the acting organization to have `parent_id IS NULL`, to hold the `white_label_branding` entitlement, and to end the write with a slug set. Replaces the `is_reseller()` check on every write surface. Parentless-ness forbids child organizations; the entitlement keeps the free plan out; the slug guarantees a branded organization always has a branded login URL to hand out. Each condition fails with its own distinguishable reason — the first is permanent, the second is a billing state, the third is a step the admin can take right now. |
| **Enforcement layer** | Service/permission layer, per surface — REST view, GraphQL mutation, admin form. No model-level `clean()` and no database constraint. A cross-row rule on `parent_id` would need a trigger rather than a `CHECK`, and the write surfaces are a closed, enumerable set. The cost is that a future fourth surface must remember; the shared helper below keeps that cheap. |
| **Shared gate helper** | One function in the organizations app, called by all three surfaces, so the rule has a single definition even though enforcement is per-surface. Each surface translates its refusal into its own error idiom (DRF `PermissionDenied`, `GraphQLError`, admin `ValidationError`). The helper resolves the entitlement service through the DI framework (`@inject` + `Annotated[EntitlementService, Provide["entitlement_service"]] = None`, fail-closed), not by importing `di_core.containers.container` as a service locator — amended 2026-08-05, applies to every branding entitlement helper in Phases 2b/3/4. |
| **Logo storage** | The upload replaces `logo_url` rather than sitting beside it. One source of truth, and we stop rendering an arbitrary third-party URL on our own login page — hotlinking a URL an organization controls means whatever it points at today can become something else tomorrow, on a page carrying our session. |
| **Logo upload path** | Reuse the shipped `s3direct` signing view with a new destination rather than writing our own endpoint, plus a GraphQL mutation returning the same signed payload for partner-API callers. The destination's `auth` callable is tightened from bare `is_authenticated` to the branding-eligible check, so the signing surface is not open to every logged-in user on the platform. That callable receives only the user, so it authorizes "this user administers some branding-eligible organization" rather than "acting for this specific organization" — accepted, because the generated key is unique per upload and the object only becomes visible once a branding row references it. |
| **Logo delivery** | An unauthenticated route on our domain streams the object; the bucket stays private and no signed URL ever reaches a client. This is what makes the logo work in an invitation email opened days later, which a 2-hour signed URL cannot. The route is keyed on the organization's slug, not on an object key, so it resolves slug → branding row → stored key and can only ever serve an object some branding row references. An unknown slug returns our default logo, matching `brandingForTenant`'s no-enumeration-oracle behavior. |
| **Logo delivery caching** | `Cache-Control` with a short max-age plus an `ETag` derived from the stored key. The route's URL is stable across re-uploads, so a long max-age would pin a replaced logo in caches and in already-delivered emails. |
| **Logo limits** | Content-type allowlist of PNG, JPEG, and WebP; a maximum size enforced in the signed policy so S3 rejects an oversized body rather than us; SVG rejected outright. SVG carries script and renders on our login page, which makes it a stored-XSS surface — worth the loss of the format designers ask for most. No dimension checks: they need post-upload inspection, and the size cap already bounds the damage. |
| **Redirect storage** | A single `redirect_url` on `OrganizationBranding`, replacing `return_url_allowlist` in the same migration. No caller-supplied destination is honored, so there is nothing to validate at request time and no open-redirect surface. The old column is dropped rather than deprecated: no row has entries, so there is nothing to preserve for a revert. |
| **Redirect resolution** | The existing JSON callback keeps its shape and stops trusting the client's `callback_url` for the post-auth destination. It resolves the destination from the acting organization's branding and returns it in the response for the SPA to navigate to. No server-side 302 route is added. |
| **`validateReturnUrl`** | Deleted along with the allowlist it reads. Its only purpose was validating caller-supplied return targets, which no longer exist. Confirm zero callers before the phase merges. |
| **Public identifier** | A new unique `slug` on `Organization`, chosen self-serve by an organization admin, used by the organization-scoped login URL and by `brandingForTenant`. Organizations have only sequential integer primary keys today, and `brandingForTenant` is unauthenticated, so integer-keyed branded login URLs would let anyone walk the ID space and harvest name/logo pairs — a better phishing-kit input once every paying organization can brand. |
| **Slug is a precondition for branding** | A branding write is refused unless the organization ends the write with a slug. Stated as a property of the resulting state rather than of the prior state, which is what lets the GraphQL path set both in one call while the dashboard flow still sets the slug on the organization endpoint first. |
| **Slug is mutable** | An admin can change it; previously-issued branded login URLs stop resolving and fall back to our default identity. No history table, no reclaim policy. The alternative — keeping old slugs alive — buys URL stability at the cost of a table and a policy for a problem no customer has yet. |
| **Slug validation** | Three rules, because it is self-serve and lands in a URL path: a reserved-word list covering our own route names and names implying us; format and length limits (lowercase alphanumeric with hyphens, bounded); and rejection of mixed-script and confusable characters so one organization cannot register a visual twin of another's. The last is a phishing defense, not tidiness — a lookalike slug plus a lookalike logo is the whole attack. |
| **Login URL shape** | Path segment carrying the slug. Survives copy-paste in a way a query parameter does not, and reads as a real branded entry point. |
| **Entitlement seeds** | Untouched. The seeded plans already say what we want — free off, unlimited on — and this work is about dropping the reseller gate, not about repricing. |
| **Audit** | Branding writes go through `AuditService` with diffs on update, matching how it is wired into other business services. A branded invitation email is a plausible phishing vector, so a trail of who set what and when has value beyond bookkeeping. |
| **Capability signal** | A read-only `can_manage_branding` field on the membership/organization payload the SPA already fetches, computed as parentless-and-entitled — deliberately **not** including the slug. An organization missing only a slug should still see the branding page, with a refusal that reads as "pick a slug first" rather than a page that silently is not there. Folding the slug in would hide the page from exactly the admins who are one step away from using it. |
| **No feature flag — justified** | The spec calls for a single release. The widened branding gate is additive (an existing 403 becomes a 200 for a population that previously had no access; no existing caller changes behavior). The redirect change touches an existing flow, which would normally earn a flag, but the only consumer of that flow is our own SPA, no reseller organizations exist in production, the backend change is additive to the response body and deploys first, and revert is a clean rollback. A flag here would be flag debt with no cohort to roll out to. |
| **Deploy ordering** | This repo first, every time. The capability field and the resolved destination are additive to response bodies; the SPA ignores them until its own release. The one ordering constraint is that the SPA must stop relying on its own `callback_url` for the post-auth destination before we can claim the objective, but nothing breaks if it lags. |

## 3. Data Model Changes

### 3.1 `Organization.slug`

New field on @organizations/models.py — `SlugField`, unique, nullable (organizations
created before this, and organizations that never brand, have none). Nullable rather than
backfilled because the slug is only meaningful for organizations that expose a branded
login URL, and a generated slug for every existing organization would be noise with a
uniqueness surface — and because its absence is a meaningful state the branding gate reads.

Migration adds the column plus a unique index. Nullable with a unique index allows multiple
`NULL`s in Postgres, which is the behavior we want.

Mutable after set. Changing it orphans previously-issued branded login URLs, which then
fall back to our default identity rather than erroring — the same path an unknown slug
already takes, so no extra handling is needed for the orphaned case.

### 3.2 `OrganizationBranding.redirect_url`, and the removal of `return_url_allowlist`

On @organizations/models.py — add `redirect_url` (`URLField`, blank, default `""`), drop
`return_url_allowlist` (`ArrayField`). One migration, both operations.

`OrganizationBranding` is a one-row-per-organization side table with no hot-path reads, so
the schema change carries no lock concern worth staging.

### 3.3 `OrganizationBranding.logo`, replacing `logo_url`

On @organizations/models.py — `logo_url` (`URLField`) is replaced by an `S3DirectImageField`
pointing at a new `branding_logos` destination, following the precedent at
@users/models.py:67. Same migration as the redirect swap where convenient, or its own; both
are on the same small table.

No backfill. Any organization with a `logo_url` today is a reseller, and there are none in
production — the same emptiness that makes the allowlist drop free. Confirm before the
migration lands.

### 3.4 Branding root resolution

`Organization.get_branding_root()` in @organizations/models.py gains the parentless case.
`resolve_branding()` and `resolve_branding_for_display()` need no signature change — they
already delegate the root walk. The docstring on `resolve_branding` explaining why it is
deliberately ungated (it feeds `validate_return_url`) becomes stale once that query is
deleted; the ungated variant should be re-examined in that phase rather than left with a
docstring pointing at a function that no longer exists.

## 4. API Design

### 4.1 REST — `/branding/`

Unchanged shape, changed gate and changed fields.

- `GET` / `PUT` / `PATCH` on @organizations/views.py — `_check_reseller_status()` is
  replaced by the shared parentless-and-entitled gate.
- Refusal reasons become distinguishable: an organization with a parent and an organization
  without the entitlement are different 403 bodies, because the first is permanent and the
  second is a billing state the admin can act on.
- `return_url_allowlist` leaves the serializer; `redirect_url` enters it, validated as
  HTTPS with no wildcard and no path-prefix pattern.

### 4.2 REST — organization update

On @organizations/views.py — `OrganizationViewSet` already restricts `update` /
`partial_update` to `IsOrganizationAdmin` and scopes `get_queryset` to the caller's own
organization, so it needs no new permission work. `slug` moves from read-only to writable on
`OrganizationSerializer`, carrying the three validation rules. A collision returns 400 naming
the conflict, not a 500 from the unique index.

### 4.3 Public GraphQL — `updateBranding`

On @public_api/mutations.py — `assert_org_can_invite(acting_org)` is replaced by the shared
gate. `return_url_allowlist` leaves `UpdateBrandingInput`; `redirect_url` enters it with the
same validation. `slug` also enters the input as an optional field, so a partner-API caller
can satisfy the slug precondition in the same call rather than needing an organization-update
mutation that does not exist. When supplied it is validated and applied to the organization
before the gate's slug condition is evaluated; when omitted, the organization's stored slug
must already satisfy it.

### 4.4 Public GraphQL — `brandingForTenant` and `validateReturnUrl`

On @public_api/queries.py — `brandingForTenant` gains slug lookup alongside the existing
tenant-id argument, keeping its no-enumeration-oracle behavior (an unknown identifier
returns the default, indistinguishable from an unbranded organization).
`validateReturnUrl` is deleted with its result type.

### 4.5 Logo upload and delivery

**Signing** — the shipped `s3direct` view at `/s3direct/`, with a new `branding_logos`
destination in `S3DIRECT_DESTINATIONS`. The SPA posts to it as it would for a profile
picture. For partner-API callers, a GraphQL mutation returns the same signed payload,
authorized by the branding gate rather than by the destination's `auth` callable.

**Delivery** — a new unauthenticated route keyed on the organization slug, streaming the
stored object with caching headers. Unknown slug, no branding row, no logo, or an
unentitled organization all return our default logo, indistinguishably.

**Reads** — `logo_url` on the REST serializer, on `BrandingResult`, and on
`PublicBrandingResult` all return this route's URL rather than a raw or signed S3 URL. The
field name stays, so the SPA's read path is unchanged; only what it points at differs.

### 4.6 Auth callback

On @accounts/views.py — `ProviderCallbackAPIView` response gains the resolved
post-authentication destination. `state["next"]` continues to serve as the OAuth
`callback_url` for the token exchange, which is a protocol requirement; what changes is that
it is no longer what decides where the user ends up.

## 5. Phased Rollout

Phases are bundled by concern rather than one-per-use-case, per the granularity decision.
Read-side use-cases that share the resolution path land together.

---

### Phase 1 — Self-serve organization slug

**Goal**: an organization admin picks a public slug for their organization through the
dashboard's organization endpoint, subject to uniqueness, format, reserved-word, and
confusable-character rules.

**Feature flag**: none — additive nullable column and an additive writable field on an
endpoint whose existing fields are untouched. See **Guiding Decisions**.

Changes:
1. @organizations/models.py: add `slug` to `Organization` — unique, nullable, indexed.
2. Migration adding the column and the unique index.
3. New validation module in the organizations app holding the three rules, so the REST
   serializer and the GraphQL input in Phase 3 share one implementation:
   - **Reserved words**: our own route names (`login`, `admin`, `api`, `dashboard`, `app`,
     `auth`, `static`, `media`) and names implying us (`vinta`, `vinta-schedule`, and close
     variants). The list is data, not scattered conditionals, so it can grow without a
     rewrite.
   - **Format and length**: lowercase alphanumeric plus internal hyphens, bounded minimum
     and maximum, no leading or trailing hyphen, not purely numeric (a numeric slug would
     read as an organization id).
   - **Confusables**: reject mixed-script strings and characters that render as
     lookalikes of ASCII. This is the phishing defense — a visual twin of another
     organization's slug alongside a copied logo is the whole attack. Prefer the standard
     Unicode confusables data over a hand-rolled character list.
4. @organizations/serializers.py: `slug` becomes writable on `OrganizationSerializer` with
   the shared validation; add it read-only to `OrganizationBriefSerializer`. A uniqueness
   collision returns 400 naming the conflict rather than surfacing an integrity error.
5. @organizations/admin.py: expose `slug`, searchable, running the same validation through
   the admin form.
6. Regenerate `schema.yml`.

Spec use-case: supports **Use-case 3** (a returning user of a branded organization signs in)
— this is the half an admin performs.

Tests:
- **Unit**: new test module for the validation rules — each reserved word rejected; format
  violations rejected one per rule; a mixed-script lookalike of an existing slug rejected;
  a plain valid slug accepted. Table-driven, since the value is in the breadth of cases.
  @organizations/tests/test_models.py — uniqueness holds, multiple `NULL` slugs coexist,
  slug is optional on creation.
- **Integration**: @organizations/tests/test_models.py or the organization endpoint tests —
  an admin sets a slug through `PATCH`; a non-admin member is refused; a second organization
  claiming the same slug gets 400 with a message naming the collision, not a 500; changing
  an existing slug succeeds and the old value stops resolving.
  @organizations/tests/test_organization_admin.py — the same rules apply in admin.
- **Route/reserved-slug sync guard** (added by the 2026-08-05 amendment): a test that
  enumerates the project's top-level URL route segments and asserts every one of them is
  present in the reserved-slug set, so a future route added without updating the reserved
  list fails CI. This answers the review question on keeping the two in sync: enforcement
  lives in the test suite, not in a reviewer's memory. The reverse direction (a slug already
  taken when a route of the same name is later added) is a deliberate non-goal — routes are
  ours to name and the reserved list is the one-way guard; the test documents that stance.

**Review models**: reviewer Tier 3 — the confusables and reserved-word rules are the phishing
control for the whole feature, and a permissive gap in either is invisible until someone
exploits it.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Not the boilerplate this phase looked like before the slug became self-serve — confusable detection has real subtlety and the rule set needs to be designed, not pattern-matched.

**Reusable skills**: `add-migration`.

Acceptance: an organization admin sets a unique slug through the organization endpoint; a
reserved word, a malformed value, a mixed-script lookalike, and a duplicate are each rejected
with a message naming the rule; a non-admin cannot set one.

---

### Phase 2a — Swap the allowlist for a single redirect destination

**Goal**: branding stores one redirect destination instead of a list, and the public query
that validated caller-supplied return targets is gone. Ship value: none user-visible — this
is the contract change every later phase writes against.

**Feature flag**: none — the replaced surface has no production data and no confirmed
callers. See **Guiding Decisions**.

Changes:
1. @organizations/models.py: add `redirect_url` to `OrganizationBranding`, remove
   `return_url_allowlist`.
2. Migration performing both operations.
3. @organizations/serializers.py: `redirect_url` replaces `return_url_allowlist` in
   `OrganizationBrandingSerializer`; `validate_return_url_allowlist` is replaced by a
   `validate_redirect_url` enforcing HTTPS scheme, no wildcard character, and no
   path-prefix pattern.
4. @organizations/admin.py: swap the field in `OrganizationBrandingAdmin`'s fieldset.
5. @public_api/types.py: `redirect_url` replaces `return_url_allowlist` on
   `UpdateBrandingInput`; delete the `validateReturnUrl` result type.
6. @public_api/mutations.py: same swap plus the same validation in `update_branding`.
7. @public_api/queries.py: delete `validate_return_url`.
8. @organizations/models.py: `resolve_branding`'s docstring justified its ungated status by
   pointing at `validate_return_url`. With that gone, re-examine whether the ungated variant
   still has a caller; if it does not, note it for removal rather than leaving a function
   whose rationale references deleted code.
9. Regenerate `schema.yml` and the GraphQL schema surface snapshot.

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @organizations/tests/test_branding.py — `redirect_url` validation accepts a
  plain HTTPS URL and rejects `http://`, a wildcard, and a path-prefix pattern, each with a
  message naming the rule.
- **Integration**: @public_api/tests/test_queries.py — `validateReturnUrl` is absent from
  the schema; @public_api/tests/test_schema_surface.py — the surface snapshot reflects both
  the removed query and the swapped input field.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Mechanical field swap across five modules plus new validation with a small edge set.

**Reusable skills**: `add-migration`.

Acceptance: `grep -r "return_url_allowlist"` over the app returns nothing, `validateReturnUrl` is
absent from the published schema, and a non-HTTPS or wildcard `redirect_url` is rejected
with a message naming the rule.

---

### Phase 2b — Logo upload and delivery

**Goal**: an organization admin uploads a logo straight to S3 from the dashboard or through
the public API, and it renders on unauthenticated pages and in invitation emails read at any
later time. Reseller organizations get the full path on merge; plain organizations upload
successfully but still resolve to our default logo until Phase 5 widens resolution. That is
the ordering cost of landing the storage contract before the resolution change, and nothing
breaks in between.

**Feature flag**: none — a new destination, a new route, and a field swap on a table with no
production rows. See **Guiding Decisions**.

Changes:
1. @organizations/permissions.py: introduce the shared eligibility helper — acting
   organization is parentless and holds `white_label_branding`. This phase is the first
   caller; Phase 3 extends it with the slug condition and wires it into the write surfaces.
   Introduced here rather than in Phase 3 so the signing surface is never the loose version.
   The entitlement lookup is obtained through the DI framework — `@inject` plus an
   `Annotated[EntitlementService, Provide["entitlement_service"]] = None` parameter,
   fail-closed when the service is unresolvable — following the established pattern
   (`audit/services.py`, `accounts/account_adapters.py`); the `organizations` package is
   already wired via `container.wire(packages=INTERNAL_INSTALLED_APPS)`. Do NOT reach into
   `di_core.containers.container` as a service locator. (Clarified by the 2026-08-05
   amendment; the same rule governs the helpers Phase 3 and Phase 4 add.)
2. @vinta_schedule_api/settings/base.py: add a `branding_logos` entry to
   `S3DIRECT_DESTINATIONS` — private ACL, its own key prefix, content-type allowlist of
   PNG/JPEG/WebP, a maximum size in the signed policy, and an `auth` callable tightened from
   `is_authenticated` to the eligibility helper above. The local and test settings loops that
   rewrite bucket/endpoint/region per destination already iterate every key, so the new entry
   is picked up without touching them — verify rather than assume.
3. @organizations/models.py: replace `logo_url` with an `S3DirectImageField` on the
   `branding_logos` destination, following @users/models.py:67.
4. Migration for the field swap.
5. New unauthenticated delivery route in the organizations app: resolve slug → organization
   → branding row → stored key, stream the object with `Cache-Control` and an `ETag` derived
   from the key. Unknown slug, absent row, absent logo, and unentitled organization all
   return our default logo along the same path, so the route is not an existence oracle.
   It resolves only through a branding row, so it cannot be pointed at an arbitrary key.
   Resolution goes through `resolve_branding_for_display`, so the route inherits the widened
   root automatically when Phase 5 lands — no second change here.
6. @organizations/serializers.py: the branding serializer's `logo_url` becomes the delivery
   route's URL on read and accepts the uploaded key on write.
7. @public_api/types.py and @public_api/mutations.py: same on `UpdateBrandingInput`,
   `BrandingResult`, and `PublicBrandingResult`; add a mutation returning the signed upload
   payload, authorized by the eligibility helper.
8. @organizations/notification_contexts.py: the branding context's logo becomes the delivery
   route's absolute URL, so an email opened later still renders it.
9. Regenerate `schema.yml` and the GraphQL schema surface snapshot.

Spec use-case: **Use-case 1** (an administrator brands their organization), logo half.

Tests:
- **Unit**: @organizations/tests/test_branding.py — the destination's `auth` callable admits
  an admin of an eligible organization and refuses a free-plan user, a non-admin, and an
  admin of an organization with a parent. Content-type allowlist accepts PNG, JPEG, WebP and
  rejects SVG explicitly, since SVG is the one a reviewer will assume works.
- **Integration**: new test module for the delivery route — a branded organization's logo
  streams with caching headers; an unknown slug, an organization with no branding row, one
  with no logo, and an unentitled one each return the default identically; the route cannot
  be induced to serve an object no branding row references.
  @public_api/tests/test_queries.py — the signing mutation returns a payload for an eligible
  caller and refuses the same four cases as the gate.
  @organizations/tests/test_branding.py — the invitation email context carries an absolute
  delivery URL, not a signed S3 URL and not a bare key.

**Review models**: reviewer Tier 4 — this adds an unauthenticated route that reads from our
media bucket and a signing surface that hands out write credentials to it. The defects to
hunt are traversal from the slug to an unintended object, an oracle that distinguishes an
unbranded organization from a nonexistent one, and a signing path that skips the gate.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Multi-file work spanning settings, model, route, two API surfaces, and email rendering, against a library whose conventions have to be read rather than recalled.

**Reusable skills**: `add-migration`; `create-rest-endpoint` for the delivery route.

Acceptance: an eligible admin obtains a signed payload, uploads a PNG directly to S3, and the
logo renders through our delivery route on an unauthenticated request and in a rendered
invitation email; an SVG and an oversized file are rejected; an ineligible caller cannot
obtain a payload; no signed S3 URL appears in any response.

---

### Phase 3 — Widen the write gate to parentless entitled organizations

**Goal**: an admin of any paying organization with no parent can save branding; an admin of
an organization with a parent is refused by the backend on every surface.

**Feature flag**: none — an existing 403 becomes a 200 for a population that previously had
no access. No existing caller changes behavior. See **Guiding Decisions**.

Changes:
1. @organizations/permissions.py: extend the eligibility helper from Phase 2b into the full
   write gate — acting organization is parentless, holds `white_label_branding`, and ends the
   write with a slug set. Returns a distinguishable reason per failure so callers can tell the
   permanent case from the billing case from the one-step-away case. The logo signing surface
   keeps using the two-condition helper: requiring a slug before an admin can upload a logo
   would order the branding form around an implementation detail.
2. @organizations/views.py: `OrganizationBrandingView._check_reseller_status` is replaced by
   the shared gate on all three methods. A missing slug refuses with a reason the dashboard
   can render as "pick a slug first" — the REST flow never sets a slug here, it expects one
   from the organization endpoint in Phase 1. Update the class docstring, which currently
   documents the reseller-admin gate.
3. @public_api/types.py and @public_api/mutations.py: `update_branding` swaps
   `assert_org_can_invite` for the shared gate, and `UpdateBrandingInput` gains an optional
   `slug`. When supplied, it is validated with Phase 1's shared rules and applied to the
   organization before the gate's slug condition is evaluated, so a partner-API caller can
   satisfy the precondition in one call. When omitted, the stored slug must already satisfy
   it. Both writes land in one transaction — a rejected branding write must not leave a new
   slug behind.
4. @organizations/admin.py: `OrganizationBrandingAdmin` gets a form `clean` refusing an
   organization with a parent. Admin is not an escape hatch — the rule holds for staff too.
5. @organizations/exceptions.py: refusal reasons if the app's error idiom wants them named.

Spec use-case: **Use-case 1** (an administrator brands their organization) and
**Use-case 5** (an administrator of a child organization looks for branding). Bundled: they
are the two halves of one gate, and splitting them would ship a phase where the gate admits
everyone.

Tests:
- **Unit**: @organizations/tests/test_branding.py — the gate admits a parentless, entitled,
  slugged organization; refuses on parent present, on entitlement absent, and on slug
  absent; and gives a different reason for each of the three refusals.
- **Integration**: @organizations/tests/test_branding_rest.py — a parentless entitled
  slugged non-reseller admin completes `GET`/`PUT`/`PATCH`; an admin of an organization with
  a parent gets 403 on all three; an organization with no slug gets 403 with the
  pick-a-slug reason; a non-admin member of an eligible organization still gets 403; a
  free-plan organization gets 403.
  @public_api/tests/test_queries.py — `updateBranding` mirrors those cases, plus: supplying
  `slug` alongside branding on an organization that has none succeeds and sets both;
  supplying an invalid `slug` rejects the whole call and leaves the organization without one;
  supplying a `slug` that collides rejects without partially applying.
  @organizations/tests/test_organization_admin.py — saving branding for an organization with
  a parent fails validation in admin.
  A reseller organization continues to pass the gate unchanged, since a reseller is
  parentless in every fixture we have — but it now needs a slug, which is a behavior change
  for reseller fixtures and should be asserted explicitly.

**Review models**: reviewer Tier 4 — this phase is the authorization boundary for the whole
feature, it rewrites the gate on three independent surfaces, and a gap on any one of them
means an organization brands itself when the spec says it must not. Fixer left on the
project default.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Multi-file orchestration across REST, GraphQL, and admin with branching refusal semantics and a wide test matrix.

**Reusable skills**: none — no clean match.

Acceptance: an admin of a parentless, entitled, slugged, non-reseller organization saves
branding through the REST endpoint and the public mutation; an admin of an organization with
a parent is refused by all three surfaces including admin; an organization with no slug is
refused with a reason distinct from the other two; a partner-API caller can set slug and
branding in one `updateBranding` call, all-or-nothing.

---

### Phase 4 — Audit branding writes and expose the capability field

**Goal**: every branding write is recorded with a diff, and the dashboard can tell whether
to show the branding page without probing.

**Feature flag**: none — additive audit records and an additive read-only response field.

Changes:
1. @organizations/views.py and @public_api/mutations.py: record branding create and update
   through `AuditService.record`, with diffs on update, using the actor helper matching each
   surface (`actor_from_user_or_token` for REST, the system-user helper for the partner API).
2. @organizations/serializers.py: add read-only `can_manage_branding` to
   `MyMembershipSerializer` and `CurrentMembershipSerializer`, computed as
   parentless-and-entitled. This deliberately excludes the slug condition — see **Guiding
   Decisions** — so an organization one step away from branding still sees the page and the
   refusal that tells it what to do. Share the parentless-and-entitled half with the gate
   rather than restating it.
3. Regenerate `schema.yml`.

Spec use-case: supports **Use-case 1** and **Use-case 5** — the capability field is what
makes the child organization's branding page absent rather than merely refused.

Tests:
- **Unit**: @organizations/tests/test_branding.py — `can_manage_branding` is true only for
  parentless-and-entitled, and tracks the gate rather than duplicating it.
- **Integration**: @audit/tests — a branding create writes one audit entry with the acting
  organization and actor; an update writes an entry whose diff names only changed fields;
  a refused write records nothing.
  @organizations/tests/test_branding_rest.py — the membership payload carries
  `can_manage_branding` for each of the four gate cases.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Established audit-wiring pattern plus a computed serializer field.

**Reusable skills**: none — no clean match.

Acceptance: creating and updating branding produces audit entries with correct actor and
diff, and the membership payload's `can_manage_branding` is true exactly when the write gate
would admit the caller.

---

### Phase 5 — Resolve branding for parentless organizations

**Goal**: a branded parentless organization's identity appears on its invitation accept page
and its invitation email. This is the phase that delivers the feature's visible outcome.

**Feature flag**: none — an organization that has configured nothing resolves exactly as it
does today. See **Guiding Decisions**.

Changes:
1. @organizations/models.py: `get_branding_root()` returns the nearest reseller ancestor if
   one exists, otherwise `self` when `parent_id` is `NULL`, otherwise `None`. The reseller
   branch is checked first and is unchanged, which is what preserves reseller precedence.
2. @organizations/notification_contexts.py: no logic change expected — it already calls
   `resolve_branding_for_display`, which inherits the widened root. Verify rather than
   assume; the module's branding-context construction should be read against the new root
   semantics.
3. @public_api/queries.py: `branding_for_tenant` inherits the widened root; add slug lookup
   alongside the tenant-id argument, preserving the default-on-unknown behavior for both.

Spec use-case: **Use-case 2** (invited user accepts from a branded organization),
**Use-case 4** (invited user of a reseller's child accepts), and **Use-case 6** (a branded
organization downgrades). Bundled: all three are the same resolution call reaching different
branches, and separating them would produce three phases whose diffs are one test file each.

Tests:
- **Unit**: @organizations/tests/test_branding.py — root resolution for a parentless
  non-reseller returns itself; for a child under a reseller returns the reseller; for a child
  under a non-reseller parent returns `None`; for an unentitled parentless organization the
  display variant returns `None` while values remain in the database.
- **Integration**: @organizations/tests/test_branding.py — the invitation email context
  carries the organization's app name, logo, and colors for a branded parentless
  organization, and our defaults for an unentitled one whose row still exists.
  @public_api/tests/test_queries.py — `brandingForTenant` returns the organization's branding
  by id and by slug, and the default for an unknown value of either.

**Review models**: reviewer Tier 4 — this changes the return value of the single function
every branding caller depends on, including an unauthenticated public query. A mistake here
either leaks one organization's branding onto another's page or silently reverts resellers
to defaults.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Small diff, wide blast radius, and a resolution matrix that has to be reasoned through rather than pattern-matched.

**Reusable skills**: none — no clean match.

Acceptance: an invited user of a branded parentless organization receives an email and sees
an accept page carrying that organization's identity; a child organization under a branded
reseller still renders the reseller's; an unentitled organization renders ours with its saved
values intact.

---

### Phase 6 — Route invitation replies to the organization

**Goal**: replies to a branded organization's invitation email reach that organization, while
the From address stays on our sending domain.

**Feature flag**: none — organizations without branding keep today's sender exactly.

Changes:
1. @organizations/notification_contexts.py: the resolved `support_email` becomes the
   reply-to on branded invitations. The From address is untouched — no custom sender, no
   domain verification, per **Non-goals**.
2. Whichever notification backend constructs the outbound message: thread reply-to through.
   If the current path has no reply-to concept, that plumbing is this phase's real work.

Spec use-case: **Use-case 2**, reply-to half.

Tests:
- **Integration**: @organizations/tests/test_branding.py — a branded organization's
  invitation carries its support address as reply-to and our address as From; an unbranded
  or unentitled organization's invitation carries our address in both, byte-for-byte with
  today.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Standard pattern application, with the caveat that the reply-to plumbing may not exist yet.

**Reusable skills**: none — no clean match.

Acceptance: a branded organization's invitation email has that organization's support
address as reply-to and our address as From; an unbranded organization's email is unchanged
from today.

---

### Phase 7 — Resolve the post-authentication destination server-side

**Goal**: authentication returns the destination the organization configured, resolved by us,
never taken from the client.

**Feature flag**: none — the field is additive to the response body and the SPA ignores it
until its own release. See **Guiding Decisions**.

Changes:
1. @accounts/views.py: `ProviderCallbackAPIView` resolves the acting organization's branding
   and includes the resulting destination in its response — the configured `redirect_url`
   when the organization is entitled and has one, our dashboard otherwise. `state["next"]`
   keeps serving as the OAuth `callback_url` for the token exchange; it stops deciding where
   the user lands.
2. @accounts/views.py: structured log per completion recording the organization and whether
   the destination resolved to a configured value or the dashboard fallback, per the
   observability decision. No alerting.

Spec use-case: **Use-case 2** step 4, **Use-case 3** step 3, and the spec's
**Acceptance scenario 8** (reseller redirect under the unified route).

Tests:
- **Integration**: @accounts/tests — the callback returns the configured destination for a
  branded entitled organization; the dashboard for an unbranded one, for an unentitled one
  whose row still exists, and for one with no `redirect_url`; the reseller fixture returns the
  reseller's destination. A client-supplied `callback_url` pointing somewhere else does not
  change the returned destination.

**Review models**: reviewer Tier 4 — this is an authentication path, and the specific defect
to hunt for is any residual path where a client-supplied value still reaches the returned
destination. That is an open-redirect, and it is the thing the whole allowlist replacement
was meant to eliminate.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Allauth headless internals plus resolution branching; the surrounding code is dense enough that pattern-matching will not carry it.

**Reusable skills**: none — no clean match.

Acceptance: the callback response carries the organization's configured destination when
entitled and configured, our dashboard in every other case, and a client-supplied
`callback_url` cannot influence it.

---

### Phase 8 — Branded login by organization slug

**Goal**: a returning user opening an organization-scoped login URL sees that organization's
branding before signing in.

**Feature flag**: none — a new URL shape alongside the existing generic login, which is
untouched.

Changes:
1. Backend support for resolving an organization from a slug in the login path, so the SPA's
   branded route has something to call. The slug lookup added to `brandingForTenant` in
   Phase 5 is the query; this phase is whatever routing or context the login path needs on
   top of it.
2. Confirm the generic login path with no organization in the URL keeps rendering our
   default identity and does not infer an organization from the browser, per **Non-goals**.

Spec use-case: **Use-case 3** (a returning user of a branded organization signs in).

Tests:
- **Integration**: @public_api/tests/test_queries.py and @accounts/tests — a slug-scoped
  login resolves the branded organization; an unknown slug returns our default with no
  distinguishable error; the generic login path is unchanged.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Thin adapter over the resolution path built in Phase 5.

**Reusable skills**: none — no clean match.

Acceptance: opening the organization-scoped login URL for a branded organization renders its
identity; an unknown slug renders ours; the generic login page is unchanged.

---

### Phase 9 — Client handoff for the SPA

**Goal**: the dashboard team has everything needed to hide the branding page for ineligible
organizations, navigate to the resolved post-auth destination, and build the branded login
route — without reading this repo.

**Feature flag**: none — documentation.

Changes:
1. Produce the handoff document covering: the `can_manage_branding` field and where it
   appears; the `redirect_url` field replacing `return_url_allowlist` on both the REST
   endpoint and the GraphQL input; the removal of `validateReturnUrl`; the resolved
   destination in the callback response and the instruction to stop deciding it client-side;
   the slug field and its validation rules; the slug-scoped login route; the three
   distinguishable 403 reasons so the UI can tell a permanent refusal from a billing state
   from a missing slug; and the logo upload flow — which destination to post to, the accepted
   content types and size cap, that SVG is rejected, and that `logo_url` now returns our
   delivery route rather than a URL the organization supplied.
2. State deploy ordering explicitly: this repo first, the SPA at its own pace, nothing breaks
   while it lags.

Spec use-case: supports **Use-case 5** (the branding page must be absent, not merely refused)
and **Use-case 3**.

Tests: none — documentation phase. The contracts it documents are covered by the tests in
Phases 2 through 8.

**Suggested AI model**: Tier 1 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Mechanical extraction from a merged diff.

**Reusable skills**: `handoff-to-client`.

Acceptance: the handoff document lists every added, changed, and removed field and operation
on this branch relative to `main`, with the breaking removal of `validateReturnUrl` flagged
as breaking.

---

## 6. Risk & Rollout Notes

**No feature flag.** Justified in **Guiding Decisions**. The consequence is that rollback for
Phases 5, 7, and 8 is a revert rather than a toggle. Phases 1 through 4 are additive and
carry no rollback story beyond the migrations.

**The logo delivery route is the one new unauthenticated surface in this work.** It reads
from our media bucket on anonymous request. Two properties carry its safety and both are
tested: it resolves only through a branding row, so no caller-supplied value ever becomes an
object key; and every miss — unknown slug, no row, no logo, unentitled — returns the same
default along the same path, so it answers no questions about which organizations exist.
Whoever reviews Phase 2b should attack those two properties specifically. It has no CDN in
front of it, so a hot organization's logo is served by us on every uncached request.

**The logo signing surface hands out write credentials to our bucket.** Tightening the
destination's `auth` callable from `is_authenticated` to branding-eligible is what keeps it
from being open to every logged-in user on the platform. The callable receives only the user,
so it authorizes at user granularity rather than acting-organization granularity — an
eligible admin can obtain a payload while acting for a different organization. The uploaded
object is inert until a branding row references it, which is what makes this acceptable
rather than merely cheap.

**Migrations.** Three, all on small tables. Phase 1 adds a nullable column plus a unique index
to `organizations_organization`; Phases 2a and 2b add and drop columns on
`organizations_organizationbranding`, which holds at most one row per organization and has no
hot-path reads. No lock staging, no partition work, no query-plan concern. None needs a
backfill: the slug is deliberately null for organizations that never brand, the dropped
allowlist has no rows with entries, and no production row has a `logo_url` to migrate into
the new field.

**Confirm before Phase 2 merges** that `validateReturnUrl` has no callers. The spec assumes
none because we have no partner integrations, but this is a lookup, not a negotiation, and it
is the one irreversible step in the plan.

**Rollback.** Phase 2's dropped column cannot be restored with its data by a code revert. The
data is empty, which is why the drop was chosen, but that assumption should be verified
against production immediately before merge rather than trusted from the spec.

**Deploy ordering.** This repo first in all cases. Every backend change is additive to
response bodies the SPA already receives. The SPA lagging means the branding page stays
hidden and the post-auth destination stays client-decided — degraded, not broken.

**What the objective actually depends on.** Objective 1 in the spec — zero vendor references
on the invited-user path — is not fully satisfied by this repo. The accept page is rendered
by the SPA. This plan delivers the data and the contracts; the walkthrough that proves the
objective can only be run after the SPA ships its side.

## 7. Open Questions

1. **Does a per-organization reply-to address need verification?** Nothing stops an admin
   entering an address they do not control. Recommended default: no verification in this
   scope, revisit if abuse appears. Owner: whoever owns email deliverability. Blocks nothing;
   decide before Phase 6 merges.
2. **Is there any review of what organizations put in the app name and logo?** Open to every
   paying customer, a branded invitation email is a workable phishing template. Recommended
   default: no pre-publication review, takedown on report. Owner: trust and safety. Blocks
   nothing; decide before release.
3. **What is on the reserved-slug list, and who maintains it?** The plan seeds it with our
   route names and names implying us, but the list is a product asset once slugs are
   self-serve — a competitor's name, a well-known brand, or a future route we have not built
   yet are all arguable entries. Recommended default: ship the route-and-vendor seed, review
   the list when the first rejection complaint arrives. Owner: product. Blocks nothing.
4. **What happens to an organization that releases a slug another organization then claims?**
   Slugs are mutable with no reclaim policy, so an admin can free a slug and a second
   organization can take it, inheriting whatever branded login URLs are still circulating for
   the first. Recommended default: accept it — the URL shows the new organization's branding,
   which is correct for whoever owns the slug now. Owner: product. Blocks nothing; worth a
   decision before the first slug change in production.
5. **Does `resolve_branding`'s ungated variant still have a caller after `validateReturnUrl`
   is deleted?** If not, it should be removed rather than left with a docstring citing
   deleted code. Answerable during Phase 2 by reading the callers. Blocks nothing.

## 8. Touch List

**Phase 1 — self-serve organization slug**
- [organizations/models.py](../organizations/models.py) — edited
- @organizations/migrations/ — new migration
- @organizations/slug_validation.py — new module holding the three rules and the reserved list
- [organizations/admin.py](../organizations/admin.py) — edited
- [organizations/serializers.py](../organizations/serializers.py) — edited
- @organizations/tests/test_slug_validation.py — new
- [organizations/tests/test_models.py](../organizations/tests/test_models.py) — edited
- [organizations/tests/test_organization_admin.py](../organizations/tests/test_organization_admin.py) — edited
- `schema.yml` — regenerated

**Phase 2a — redirect destination swap**
- [organizations/models.py](../organizations/models.py) — edited
- @organizations/migrations/ — new migration
- [organizations/serializers.py](../organizations/serializers.py) — edited
- [organizations/admin.py](../organizations/admin.py) — edited
- [public_api/types.py](../public_api/types.py) — edited
- [public_api/mutations.py](../public_api/mutations.py) — edited
- [public_api/queries.py](../public_api/queries.py) — edited
- [organizations/tests/test_branding.py](../organizations/tests/test_branding.py) — edited
- [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — edited
- [public_api/tests/test_schema_surface.py](../public_api/tests/test_schema_surface.py) — edited
- `schema.yml` — regenerated

**Phase 2b — logo upload and delivery**
- [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py) — edited (`branding_logos` destination)
- [organizations/permissions.py](../organizations/permissions.py) — edited (eligibility helper introduced)
- [organizations/models.py](../organizations/models.py) — edited (`logo` replaces `logo_url`)
- @organizations/migrations/ — new migration
- [organizations/views.py](../organizations/views.py) — edited (delivery route)
- [organizations/routes.py](../organizations/routes.py) — edited
- [organizations/serializers.py](../organizations/serializers.py) — edited
- [organizations/notification_contexts.py](../organizations/notification_contexts.py) — edited
- [public_api/types.py](../public_api/types.py) — edited
- [public_api/mutations.py](../public_api/mutations.py) — edited (signing mutation)
- @organizations/tests/test_branding_logo.py — new
- [organizations/tests/test_branding.py](../organizations/tests/test_branding.py) — edited
- [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — edited
- `schema.yml` — regenerated

**Phase 3 — widened write gate**
- [organizations/permissions.py](../organizations/permissions.py) — edited
- [organizations/views.py](../organizations/views.py) — edited
- [organizations/admin.py](../organizations/admin.py) — edited
- [organizations/exceptions.py](../organizations/exceptions.py) — edited
- [public_api/types.py](../public_api/types.py) — edited (`slug` on `UpdateBrandingInput`)
- [public_api/mutations.py](../public_api/mutations.py) — edited
- [organizations/tests/test_branding.py](../organizations/tests/test_branding.py) — edited
- [organizations/tests/test_branding_rest.py](../organizations/tests/test_branding_rest.py) — edited
- [organizations/tests/test_organization_admin.py](../organizations/tests/test_organization_admin.py) — edited
- [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — edited

**Phase 4 — audit and capability field**
- [organizations/views.py](../organizations/views.py) — edited
- [organizations/serializers.py](../organizations/serializers.py) — edited
- [public_api/mutations.py](../public_api/mutations.py) — edited
- @audit/tests/ — new test module for branding audit coverage
- [organizations/tests/test_branding_rest.py](../organizations/tests/test_branding_rest.py) — edited
- `schema.yml` — regenerated

**Phase 5 — branding resolution**
- [organizations/models.py](../organizations/models.py) — edited
- [organizations/notification_contexts.py](../organizations/notification_contexts.py) — verified, edited if needed
- [public_api/queries.py](../public_api/queries.py) — edited
- [organizations/tests/test_branding.py](../organizations/tests/test_branding.py) — edited
- [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — edited

**Phase 6 — invitation reply-to**
- [organizations/notification_contexts.py](../organizations/notification_contexts.py) — edited
- @notifications/ — edited if reply-to plumbing is missing
- [organizations/tests/test_branding.py](../organizations/tests/test_branding.py) — edited

**Phase 7 — post-auth destination**
- [accounts/views.py](../accounts/views.py) — edited
- @accounts/tests/ — new or edited test module for callback destination resolution

**Phase 8 — branded login by slug**
- [accounts/urls.py](../accounts/urls.py) — edited
- [public_api/queries.py](../public_api/queries.py) — edited
- @accounts/tests/ — edited
- [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — edited

**Phase 9 — client handoff**
- @docs/ or the handoff skill's output location — new document

## Amendments

- **2026-08-05** — Address stacked-PR review comments. (1) DI correctness (PR #210,
  `organizations/permissions.py`, reviewer hugobessa): the branding entitlement helpers must
  obtain `EntitlementService` through the DI framework (`@inject` + `Annotated[...,
  Provide["entitlement_service"]] = None`, fail-closed) instead of the
  `di_core.containers.container` service-locator. Applied at the origin of the pattern.
  Affected phases: 2b, 3, 4 (body-amended); 2a, 5, 6, 7, 8, 9 (rebased). (2) Route/reserved
  sync (PR #206, `organizations/slug_validation.py`, reviewer arthurzeras): added a Phase 1
  test enforcing that every top-level URL route segment is in the reserved-slug set.
  Affected phases: 1 (body-amended); all downstream rebased. Branches force-pushed: phase-1,
  phase-2a, phase-2b, phase-3, phase-4, phase-5, phase-6, phase-7, phase-8, phase-9.
