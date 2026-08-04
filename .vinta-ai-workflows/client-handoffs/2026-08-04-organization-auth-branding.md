# API changes: Organization Auth-Area Branding

- **Date:** 2026-08-04
- **Scope:** `plan/organization-auth-branding/phase-9` vs `main` (`ed63753..7b0731f`) — the full stack of Phases 1 through 8 of the Organization Auth-Area Branding plan
- **Audience:** Web SPA (React), Partner integrations
- **Breaking changes:** 2 (`validateReturnUrl` query removed; `return_url_allowlist` field removed everywhere it appeared)

## Summary

Any paying organization with no parent (not just resellers) can now configure its own
auth-area branding — app name, logo, colors, support address, and a single post-login
destination — gated on the `white_label_branding` entitlement and a self-serve `slug`. The
branding write path is entirely new capability for previously-ineligible organizations; the
two breaking removals affect only the return-URL-validation surface, which had no callers in
production (no reseller/partner accounts exist yet). The most important non-breaking change
for the SPA is behavioral, not additive: the OAuth callback response now carries a resolved
`destination`, and the SPA must stop deciding where a signed-in user lands from its own
`callback_url`/`next` state.

## Breaking changes

### 1. `validateReturnUrl` query removed — BREAKING

The public GraphQL root query `validateReturnUrl` and its result type `ValidateReturnUrlResult`
no longer exist in the schema. There is no replacement query — the concept it served
(validating a caller-supplied return URL against an allowlist) no longer applies now that the
backend resolves the post-auth destination itself (see [Resolved post-auth destination](#4-resolved-post-auth-destination-the-oauth-callback-now-tells-you-where-to-go)
below). Any caller of this query will get a GraphQL validation error ("Cannot query field
`validateReturnUrl` on type `Query`").

**Migration:** delete any call site. If a caller was using this to pre-check a redirect target
before sending a user through OAuth, that check is no longer meaningful — the backend no
longer accepts a caller-supplied redirect at all.

### 2. `return_url_allowlist` replaced by `redirect_url` — BREAKING

`OrganizationBranding.return_url_allowlist` (a list of allowed return-URL patterns, including
wildcard/prefix entries) is gone from every surface that exposed it:

- REST: `GET/PUT/PATCH /branding/` no longer accepts or returns `return_url_allowlist`.
- GraphQL: `UpdateBrandingInput.return_url_allowlist` no longer exists.

In its place, a single `redirect_url` field (string, one concrete HTTPS URL, not a pattern)
replaces it on both surfaces. See field details under [`redirect_url` validation](#2-redirect_url-replaces-return_url_allowlist)
below.

**Migration:** any client sending `return_url_allowlist` in a branding write must switch to
sending `redirect_url` (a single URL, not a list). Any client reading `return_url_allowlist`
back from a branding response must switch to reading `redirect_url`.

---

## REST — `GET/PUT/PATCH /branding/`

- **Status:** changed
- **Auth:** `IsAuthenticated` + active org-admin membership (`IsOrganizationAdmin`). Requires
  `X-Organization-Id` header when the caller has more than one active membership (standard
  tenant-scoping behavior for this endpoint).
- **Tags:** `Branding` in `schema.yml`.

### What changed

**Gate widened.** The endpoint used to require the acting organization to be a reseller
(`can_invite_organizations=True`). It now requires the acting organization to:

1. have no parent (`parent_id IS NULL`),
2. hold the `white_label_branding` entitlement, and
3. (write only — PUT/PATCH) already have a `slug` set on the organization.

`GET` uses a narrower two-condition gate (parentless + entitled, slug **not** required) so an
otherwise-eligible organization that just hasn't picked a slug yet can still load the branding
page and see its existing state (404 if no row yet, 200 if one exists) — see
[Three distinguishable 403 reasons](#7-three-distinguishable-403-reasons-on-branding-writes)
below for why writes and reads use different gates.

**`redirect_url` replaces `return_url_allowlist`** (breaking, see above).

**`logo_url` now returns our own delivery route**, never a raw or signed S3 URL (see
[Logo upload flow](#8-logo-upload-flow) below) — the field name is unchanged, only what it
points at.

### Request (PUT/PATCH body)

```json
{
  "app_name": "Acme Scheduling",
  "logo_url": "uploads/branding_logos/8f3c1e2a-....png",
  "primary_color": "#1A73E8",
  "secondary_color": "#FBBC04FF",
  "support_email": "support@acme.example",
  "redirect_url": "https://app.acme.example/post-login"
}
```

- `app_name` — string, required on PUT (not required on PATCH), max 120 chars.
- `logo_url` — write: the S3 key returned by the signing step (a bare key, or the full
  signed/public URL — either is normalized to a bare key server-side); empty string clears
  the logo. Read: always the delivery-route URL (see item 8).
- `primary_color` / `secondary_color` — `#RRGGBB` or `#RRGGBBAA`, optional, blank-default.
- `support_email` — optional email, blank-default.
- `redirect_url` (**new field name**, was `return_url_allowlist`) — optional, one concrete
  HTTPS URL, or `""` to clear it. See validation rules below.

### `redirect_url` validation rules

Enforced identically on this REST field and on the GraphQL `updateBranding` input (shared
validator, `organizations/redirect_url_validation.py`). Violating any rule returns HTTP 400
with `{"redirect_url": ["<message>"]}` (REST) or a `GraphQLError` naming the rule (GraphQL).
Checked in this order — the first violated rule is the one reported:

1. **No control characters** — a literal CR, LF, or tab anywhere in the value is rejected
   (blocks header/response-splitting payloads).
2. **HTTPS only** — any scheme other than `https` is rejected (`http://...` included).
3. **No wildcard character** — a literal `*` anywhere in the value is rejected.
4. **No path-prefix pattern** — a non-root path ending in `/` is rejected (e.g.
   `https://example.com/callback/` is rejected; `https://example.com/callback` is fine; the
   bare root `https://example.com` or `https://example.com/` is unaffected).
5. **Well-formed, with a host** — must otherwise pass Django's `URLValidator` restricted to
   `https`; rejects hostless values (`https://`) and scheme-confusion (`https:evil.com`).

An empty string is always valid — it means "no configured destination" (falls back to the
dashboard at authentication time).

### Response

`200`/`201` body (PUT creates with `201`, updates with `200`; PATCH is always `200`):

```json
{
  "app_name": "Acme Scheduling",
  "logo_url": "https://api.example.com/branding/logo/acme/",
  "primary_color": "#1A73E8",
  "secondary_color": "#FBBC04FF",
  "support_email": "support@acme.example",
  "redirect_url": "https://app.acme.example/post-login"
}
```

### Errors

- `400` — invalid color format, invalid `redirect_url` (see rules above), or (GET's sibling
  organization-update endpoint — see item 5) invalid/duplicate slug.
- `403` — one of three distinguishable reasons; see
  [Three distinguishable 403 reasons](#7-three-distinguishable-403-reasons-on-branding-writes).
- `404` — (GET/PATCH) organization is eligible but has no branding row yet
  (`{"detail": "Branding not yet configured for this organization."}`).

### Client migration notes

- **Web SPA:** stop sending/reading `return_url_allowlist`; switch to `redirect_url` (single
  string, not a list). Render the three 403 reasons distinctly (see item 7). Treat `logo_url`
  as an opaque, always-valid image URL — never construct or expect a raw S3 URL.
- **Partner integrations:** same field rename applies to any direct REST caller (uncommon —
  partners normally use the GraphQL `updateBranding` mutation, see below).

---

## REST — organization update: `PATCH /organizations/{id}/`

- **Status:** changed
- **Auth:** `IsAuthenticated` + `IsOrganizationAdmin`. Cross-org ids resolve to 404 (queryset
  is scoped to the caller's own organization).

### What changed

`slug` moved from **read-only** to **writable** on `OrganizationSerializer`. It also now
appears (read-only) on `OrganizationBriefSerializer`, used by `GET /organizations/mine/`.

### Request

```json
{ "slug": "acme" }
```

- `slug` — string, optional, nullable, max 63 chars. Omitting it or leaving it blank/`null`
  clears it to `NULL` (never an empty string — this is what lets any number of organizations
  coexist with no slug set). See [slug validation rules](#5-slug-validation-rules) below.

### Response

`200` — the full `OrganizationSerializer` payload, now including `slug`:

```json
{
  "id": 42,
  "name": "Acme Inc",
  "slug": "acme",
  "should_sync_rooms": false,
  "external_event_update_policy": "change_request",
  "google_service_account": null,
  "can_invite_organizations": false,
  "created": "2026-08-01T12:00:00Z",
  "modified": "2026-08-04T09:15:00Z"
}
```

### `slug` validation rules

Enforced identically on this REST field, on the GraphQL `updateBranding.slug` input, and (for
completeness) in the Django admin form (shared validator, `organizations/slug_validation.py`).
Checked in this order — confusables, then reserved words, then format/length — so each
violation reports its most specific message:

1. **Confusables** — any non-ASCII character is rejected outright (phishing defense: a
   self-serve slug lands in a URL path, and a homoglyph of another organization's slug paired
   with a copied logo is a workable phishing kit). Error names the offending character.
2. **Reserved words** — case-insensitive match against our own route names (`login`,
   `admin`, `super`, `schema`, `s3direct`, `api`, `dashboard`, `auth`, `static`, `media`,
   `settings`, `billing`, `organizations`, `graphql`, `webhooks`, `default` — the last is
   reserved because it is the logo-delivery route's own unknown-slug sentinel — and more; see
   `organizations/slug_validation.py` for the full list) plus vendor-name variants (`vinta`,
   `vintaschedule`, `vinta-software`, etc.).
3. **Format and length** — lowercase alphanumeric groups joined by single internal hyphens
   only (no leading/trailing/consecutive hyphen), 3–63 characters, and **not purely numeric**
   (a numeric slug would read as an organization id — exactly the enumerable identifier a
   slug exists to replace).

A collision with an existing organization's slug returns `400` naming the conflict:
`{"slug": ["An organization with the slug 'acme' already exists."]}` — never a 500.

Slug is **mutable**: changing it orphans any previously-issued branded login URL, which then
falls back to the default (vinta) identity, same as an unknown slug.

**Not available at organization creation** (`POST /organizations/`) — only via this PATCH
endpoint (or, for a single-call flow, via `updateBranding`'s optional `slug` input — see item 5
below).

### Client migration notes

- **Web SPA:** a settings screen for picking/changing the slug is now backed by this endpoint.
  Surface the 400 body's `slug` array as the field-level error.
- **Partner integrations:** no separate organization-update mutation exists on the public
  GraphQL surface — a partner caller sets the slug through `updateBranding`'s optional `slug`
  input in the same call as the branding write (see item 5).

---

## GraphQL — `updateBranding` mutation

- **Status:** changed
- **Auth:** `IsAuthenticated` + `OrganizationResourceAccess` — the calling token's
  `OrganizationResourceAccess` must include the `BRANDING` resource.

### What changed

The gate check switched from `assert_org_can_invite(acting_org)` (reseller-only) to the same
three-condition write gate described above (parentless, entitled, slug-set). `redirect_url`
replaces `return_url_allowlist` in `UpdateBrandingInput` (breaking, see above). A new optional
`slug` input field lets a single mutation call satisfy the slug precondition and write branding
together.

### Input — `UpdateBrandingInput`

```graphql
input UpdateBrandingInput {
  appName: String!
  logoUrl: String = ""
  primaryColor: String = ""
  secondaryColor: String = ""
  supportEmail: String = ""
  redirectUrl: String = ""
  slug: String = null
}
```

- `logoUrl` — write-only despite the name (kept for symmetry with the REST field): accepts the
  S3 key from `createBrandingLogoUpload` (or a full signed/public URL — normalized to the
  bare key). Reads never echo this back.
- `redirectUrl` — same five validation rules as the REST field (item 2 above), same shared
  validator.
- `slug` (**new**) — optional. When supplied: validated with the same rules as the REST org
  endpoint (item 5), checked for uniqueness (excluding the acting org), and applied to the
  acting organization **before** the write gate's slug condition is evaluated — so a
  partner-API caller can go from "no slug" to "branding fully configured" in one call. When
  omitted (`null`), the organization's already-stored slug must satisfy the gate on its own.
  The slug write and the branding upsert are one atomic transaction: any failure anywhere in
  the call (invalid slug, gate failure, invalid color, invalid `redirectUrl`) rolls back
  everything, including the slug change.

### Result — `UpdateBrandingResult` / `BrandingResult`

```graphql
type BrandingResult {
  id: Int!
  appName: String!
  logoUrl: String!
  primaryColor: String!
  secondaryColor: String!
}

type UpdateBrandingResult {
  branding: BrandingResult
}
```

`BrandingResult` never includes `supportEmail` or `redirectUrl` (internal-use-only fields —
email rendering and post-auth redirect resolution). `logoUrl` is always the delivery-route URL
for the acting organization.

### Errors

A `GraphQLError` is raised (not a typed error field) for each failure: gate refusal (one of the
three reasons, worded distinctly from the REST 403 bodies but preserving the same
distinguishability — see item 7), invalid/colliding slug, invalid `appName`
(empty/whitespace-only or over 120 chars), invalid color format, invalid `redirectUrl`, or an
invalid/foreign `logoUrl` key (a key outside the `branding_logos` upload prefix is rejected).

### Client migration notes

- **Partner integrations:** switch any `returnUrlAllowlist` usage to `redirectUrl` (single
  string). To onboard a slug-less organization, pass `slug` in the same `updateBranding` call
  rather than expecting a separate organization-update mutation (none exists on this surface).
  Parse the `GraphQLError` message to distinguish the three gate-refusal reasons if the UI
  needs to react differently to each (see item 7 for the exact wording per reason).

---

## GraphQL — `brandingForTenant` query

- **Status:** changed
- **Auth:** none (unauthenticated, public, rate-limited).

### What changed

Gained an optional `slug` argument alongside the existing `tenantId`. No enumeration oracle:
an unknown `slug` (like an unknown `tenantId`) returns the same default branding as an
unbranded organization — indistinguishable responses.

### Request

```graphql
query {
  brandingForTenant(slug: "acme") {
    appName
    logoUrl
    primaryColor
    secondaryColor
  }
}
```

- `tenantId: ID` (existing) and `slug: String` (**new**) — pass exactly one. When both are
  supplied, `tenantId` takes precedence. When neither resolves to a real organization (or
  neither is supplied), the vinta default branding is returned.

### Response — `PublicBrandingResult`

```json
{
  "data": {
    "brandingForTenant": {
      "appName": "Acme Scheduling",
      "logoUrl": "https://api.example.com/branding/logo/acme/",
      "primaryColor": "#1A73E8",
      "secondaryColor": "#FBBC04FF"
    }
  }
}
```

Never includes `supportEmail` or `redirectUrl`.

### Client migration notes

- **Web SPA:** this is how the branded login route (item 6) fetches the identity to render
  before authentication — call it with `slug` set from the URL path segment. An unknown slug
  renders the vinta default identity, not an error — the SPA does not need special-case
  handling for a bad/removed slug beyond rendering whatever comes back.

---

## `can_manage_branding` — new read-only field

- **Status:** added (non-breaking, additive)
- **Where it appears:**
  - REST `GET /organizations/current/` → `CurrentMembershipSerializer` (top-level field,
    alongside `role` and `organization`).
  - REST `GET /organizations/mine/` → `MyMembershipSerializer` (per-membership entry, alongside
    `role` and `organization`).
  - **Not** exposed on GraphQL — this is a REST-only, dashboard-facing capability signal
    today.

### Shape

```json
{
  "role": "admin",
  "organization": { "id": 42, "name": "Acme Inc", "slug": "acme", "...": "..." },
  "can_manage_branding": true
}
```

`can_manage_branding` is a plain boolean, always present (never `null`).

### Computation

`can_manage_branding = organization.parent_id IS NULL AND organization holds the
white_label_branding entitlement`. **Deliberately excludes the slug condition** — an
organization that is otherwise eligible but has not picked a slug yet still reports `true`
here, matching the fact that `GET /branding/` is reachable for it too (the read gate, not the
stricter write gate). This is intentional: hiding the branding page for a slug-less-but-eligible
organization would hide it from exactly the admins who are one step from using it — instead
the SPA should show the page and let a save attempt surface the "pick a slug first" refusal
(item 7).

### Client migration notes

- **Web SPA:** use `can_manage_branding` to decide whether to render the branding settings
  entry point at all (per the spec: "the branding page must be absent, not merely refused",
  for organizations that can never manage branding — has a parent, or lacks the entitlement).
  Do **not** use it to decide whether a save will succeed once the page is open — a `true`
  value can still 403 on write with the "pick a slug first" reason if no slug is set yet (see
  item 7). This field is per-membership on `mine`, computed identically regardless of the
  membership's own role (member vs admin) — write authorization (admin-only) is still enforced
  separately by the branding endpoints themselves.
- **Partner integrations:** not applicable — this field is REST-only and not present on the
  public GraphQL schema.

---

## Resolved post-auth destination — the OAuth callback now tells you where to go

- **Status:** changed (behavioral — same endpoint, same response shape plus one new field)
- **Endpoint:** the custom headless OAuth callback (`ProviderCallbackAPIView`, the non-browser
  provider-callback flow used by the SPA to complete social login).

### What changed

On a successfully completed login, the JSON response now includes a `destination` field —
the URL the client should navigate the user to next. This is resolved **entirely server-side**
from the authenticated user's organization and its stored branding, using the same
`redirect_url` configured in the branding endpoint (item 1/2). It is never influenced by
anything the client sent (`callback_url`, a query parameter, a header, or the OAuth `state`).

```json
{
  "...": "existing allauth headless authentication response fields, unchanged",
  "destination": "https://app.acme.example/post-login"
}
```

- If the acting organization has a configured, entitled `redirect_url`, `destination` is that
  URL.
- Otherwise, `destination` falls back to the platform dashboard base URL
  (`FRONTEND_BASE_URL`, a new backend setting — defaults to the local dev frontend origin,
  configured per environment).

`state["next"]` (the OAuth `client.callback_url`) is **still required and still sent** — it
remains a protocol requirement for completing the token exchange with the provider. What
changed is that it **no longer decides where the user lands** after authentication completes.

### Client migration notes — **this is the one behavior change every SPA integration must make**

- **Web SPA:** stop deciding the post-authentication destination from your own
  `callback_url`/`next` state. After a successful callback response, read `destination` from
  the response body and navigate there. Continue sending whatever `callback_url` the OAuth
  flow itself requires for the token exchange — that part of the contract is unchanged — but
  treat it as OAuth plumbing only, not as the source of truth for where the user ends up.
  This is additive to the response body, so nothing breaks if the SPA has not yet made this
  change — it simply continues navigating by its own prior logic until it does (see
  [Deploy ordering](#deploy-ordering) below for why that is safe).
- **Partner integrations:** not applicable — this is the interactive browser-based OAuth
  callback used by the first-party dashboard SPA, not a partner-API surface.

---

## Logo upload flow

- **Status:** changed (storage + delivery mechanism); logo content itself is unchanged in
  intent (still one image per organization branding row)

### Signing (where to upload)

- **Web SPA:** POST to the existing shipped `s3direct` signing endpoint,
  `POST /s3direct/get_upload_params/`, with `dest: "branding_logos"` — the same mechanism
  already used for profile pictures, just a new destination name. Authorization for this
  destination is now the branding-eligibility check (parentless + entitled), not bare
  "any authenticated user" — a user must administer at least one branding-eligible
  organization to get a signed payload at all.
- **Partner integrations:** GraphQL mutation `createBrandingLogoUpload(fileName: String!,
  fileType: String!, fileSize: Int!): BrandingLogoUploadResult!` — returns the same shape the
  signing endpoint returns (`objectKey`, `accessKeyId`, `sessionToken`, `region`, `bucket`,
  `endpoint`, `acl`), gated by the same `OrganizationResourceAccess` (`BRANDING` resource) as
  `updateBranding`, checked against the acting organization directly (org-specific, tighter
  than the SPA path's user-level check).

### Content constraints

- **Accepted content types:** `image/png`, `image/jpeg`, `image/webp` only.
- **SVG is rejected outright** — not merely unlisted. SVG can carry script and would render on
  our own login/accept pages, making it a stored-XSS vector; this is a deliberate exclusion,
  not an oversight, and there is no plan to add it back.
- **Size cap:** 5 MB (`5 * 1024 * 1024` bytes) maximum, 1 byte minimum. Both surfaces
  (signing-view destination config and the GraphQL mutation) enforce the same values.
- No dimension/resolution checks and no server-side image processing — whatever is uploaded is
  what renders, unmodified.

### Storing the result

After a successful S3 upload, submit the returned object key (or the full URL — either is
accepted and normalized to the bare key) as `logo_url` (REST) / `logoUrl` (GraphQL) in the
branding write. A submitted key that does not fall under the `branding_logos` upload prefix is
rejected with a validation error naming the rule, on both surfaces.

### Reading the logo — **`logo_url` now returns our delivery route, not an organization-supplied URL**

Every read of `logo_url` — the REST branding serializer, GraphQL `BrandingResult`, GraphQL
`PublicBrandingResult` — returns an absolute URL to our own unauthenticated delivery route:

```
GET /branding/logo/<slug>/
```

- Keyed on the organization's public `slug`, never on the S3 object key.
- **Unauthenticated**, cached (`Cache-Control: public, max-age=300` plus an `ETag`).
- Every miss condition — unknown slug, no branding row, no logo set, or an organization that
  lost the entitlement — streams the **same bundled default logo**, indistinguishably (no way
  to tell "no such organization" from "real organization, no logo" from the response alone).
- An organization with no slug (or the reserved sentinel path segment `default`) resolves
  through this same unknown-slug branch to the default logo.
- The field name (`logo_url` / `logoUrl`) is unchanged on every surface — only what the value
  points at has changed. A client that previously stored/hotlinked whatever URL the
  organization supplied should stop doing that entirely: **the organization no longer supplies
  a URL at all**, only an uploaded object.

### Client migration notes

- **Web SPA:** point the logo upload widget at `dest: "branding_logos"` on the existing
  `s3direct` flow (same mechanism as profile pictures). Reject `.svg` client-side too (better
  UX — the server rejects it either way at signing time via the content-type allowlist).
  Enforce the 5 MB cap client-side for immediate feedback; the server still enforces it
  independently. Treat `logo_url` purely as a display URL — never parse it as an S3 key,
  never construct one from it.
- **Partner integrations:** call `createBrandingLogoUpload` to get signing credentials before
  uploading directly to S3 with them, then pass the returned `objectKey` (or the resulting
  object URL) as `logoUrl` in `updateBranding`.

---

## Three distinguishable 403 reasons on branding writes

- **Status:** added (REST and GraphQL both; each renders its own error idiom)

Both write surfaces (`PUT`/`PATCH /branding/` and GraphQL `updateBranding`) now refuse a write
for one of exactly three reasons, checked in this order (permanent first, billing second,
one-step-away last) so a permanent refusal is never masked by a fixable one:

| Reason | Meaning | Fixable by the org? | REST | REST body (`detail`) | GraphQL error message |
|---|---|---|---|---|---|
| **Has a parent** | Branding within a hierarchy belongs to the reseller alone; this organization can never brand itself, no matter what changes. | No — permanent. | `403` | `"This organization has a parent organization and cannot manage its own branding. Branding for organizations inside a hierarchy is controlled by the reseller organization above them."` | Same wording. |
| **Not entitled** | The organization's current plan does not include the `white_label_branding` entitlement. | Yes — upgrade plan. | `403` | `"This organization's plan does not include white-label branding."` | Same wording. |
| **No slug** | Otherwise eligible, but has not picked a public slug yet. | Yes — pick a slug right now. | `403` | `"Pick a public slug for this organization before configuring branding."` | `"Pick a public slug for this organization before configuring branding. Supply \`slug\` on this mutation, or set one via the organization endpoint first."` |

`GET /branding/` (read) uses a **narrower, two-condition gate** — parentless + entitled only,
**no slug requirement** — so a slug-less-but-otherwise-eligible organization can still load the
branding settings page (its normal 404-no-row-yet / 200-with-a-row behavior) rather than being
403'd on the very page that would let it pick a slug. Only the has-parent and not-entitled
reasons can ever surface on a GET.

### Client migration notes

- **Web SPA:** branch UI on the 403 body's `detail` text (REST) or the `GraphQLError` message
  (GraphQL) to render each reason distinctly — e.g. a permanent "not available for this
  organization" state for has-parent, a "upgrade your plan" call-to-action for not-entitled,
  and a "pick a slug first" prompt (linking to the slug field on the organization settings
  screen) for no-slug. Do not treat all three as one generic "forbidden" state — that is
  exactly what this change exists to let the UI avoid.
- **Partner integrations:** same three reasons, GraphQL wording as shown above. Recommend
  matching on distinguishing substrings (`"has a parent"`, `"white-label branding"`, `"public
  slug"`) rather than exact string equality, since wording may be refined over time while the
  three-way distinction itself is a stable contract.

---

## Slug-scoped branded login

- **Status:** added — client-side route, backed by existing/adjacent server capability
- No new server-side login route was added in this repo. The pieces the SPA needs to build a
  `/login/<slug>/`-shaped route already exist:
  - `slug` is readable on `OrganizationBriefSerializer` (`GET /organizations/mine/`, for
    the org switcher / internal linking) and on `OrganizationSerializer` (`GET/PATCH
    /organizations/{id}/`).
  - `brandingForTenant(slug: "...")` (item on `brandingForTenant` above) resolves the
    identity to render on that route before the user authenticates — an unknown slug renders
    the default (vinta) identity, not an error.
  - The generic login page (no organization in the URL) is **unaffected and unchanged** — it
    continues to show the default vinta identity. The callback flow does not read any
    organization hint from the request (header, `Referer`, or cookie) to select which
    branding to use for a slug-scoped login attempt — it resolves purely from
    `request.user`'s own membership after authentication completes (see [Resolved
    post-auth destination](#4-resolved-post-auth-destination-the-oauth-callback-now-tells-you-where-to-go)).

### Client migration notes

- **Web SPA:** build the `/login/<slug>/` (or equivalent) route entirely client-side: parse
  the `slug` path segment, call `brandingForTenant(slug: ...)` to render the branded identity
  (logo, app name, colors) on the login screen, then run the normal (unchanged) OAuth flow.
  There is no backend concept of "logging in scoped to an organization" — the slug only
  drives which identity is rendered before authentication; which organization the user
  actually lands in afterward is determined entirely by their own membership, as always.
- **Partner integrations:** not applicable — this is a first-party SPA login-page concern.

---

## Other contract changes

- **`FRONTEND_BASE_URL`** — new backend setting (`vinta_schedule_api/settings/base.py`,
  configurable via env var, defaults to `http://localhost:3000` in dev) used as the fallback
  `destination` when no organization-specific `redirect_url` is configured. Deployed
  environments should set this to the real dashboard origin.
- No REST/GraphQL rate-limit, versioning, or webhook-payload changes in this branch.
- Django admin gained equivalent slug/branding write-gate enforcement (three-condition gate on
  the branding admin form) — internal tooling only, not part of either client's API contract,
  not covered further here.
- Branded invitation emails may now carry a `Reply-To` header set to the branding root's
  `support_email` when one is configured — an outbound-email behavior change, not an API
  response change; no client-facing field or endpoint changed as a result.

## Rollout

**This repo deploys first, every time.** Every backend change described above is additive to
response bodies the SPA already receives — new fields appear alongside existing ones; nothing
existing was removed except the two items called out under **Breaking changes**, and neither
of those had any production caller (no reseller/partner organizations exist yet, and
`validateReturnUrl` was confirmed to have zero callers before it was removed). The SPA can
adopt each piece at its own pace with no coordination window required:

- Ignoring `can_manage_branding`, the new `slug` field, and the three distinguishable 403
  reasons costs the SPA a worse UX (a hidden capability it could show, or a generic error
  where a specific one is available) but breaks nothing.
- Ignoring the new logo-delivery mechanics is not possible to get wrong passively — `logo_url`
  is still a valid, renderable URL; the SPA does not need to change anything to keep reading
  it exactly as before.
- **The one exception:** the SPA **must** stop deciding the post-authentication destination
  from its own `callback_url`/`next` client-side state before the objective behind this whole
  effort — a fully server-resolved, branding-aware post-login landing — is achieved. Nothing
  breaks while the SPA lags on this specific change (the old client-side navigation logic
  keeps working, just without honoring an organization's configured `redirect_url`), but the
  feature is incomplete until it ships.

No feature flag gates any of this — see the plan's Guiding Decisions for why a flag was judged
unnecessary (the widened gate is purely additive for a previously-locked-out population, and
no reseller/partner traffic exists to protect against a behavior change in the redirect path).

For the exact machine-readable REST shapes, regenerate/consult `schema.yml`
(`make update_schema`) — this document's REST examples were checked against the branch's
regenerated `schema.yml`. The GraphQL half of this change is not covered by `schema.yml`;
consult `public_api/types.py` / `public_api/mutations.py` / `public_api/queries.py` directly,
or introspect the live `/graphql/` schema, if this document and the running schema ever
diverge.
