# Public API Documentation Site — Implementation Plan

> No `..._SPEC.md` sibling exists. This plan encodes the decisions gathered in the planning
> session (2026-07-16) directly. If a spec is authored later, reconcile the **Use-cases**
> list below against it.

## 1. Goals

1. Ship a **public, in-app `/docs` developer documentation section** (outside the `(app)` auth group) styled with `vinta-schedule-design-system`, matching the existing public-page pattern (`src/app/privacy`, `src/app/terms`).
2. Document the **public GraphQL API** (`POST /graphql/`) across four content areas: a **schema reference** derived from live introspection, a **getting-started + auth guide**, **concept guides**, and a **webhooks reference**.
3. Provide a **live "try it" GraphiQL explorer** with a public-API-token header field, calling the real `/graphql/` endpoint.
4. **Wire the landing-page placeholders** (currently `href='#'`) to the new docs, so the "Read the API docs" button and the footer Developers/Product links resolve.

**Non-goals (v1):**

- **Full-text search** — static sidebar nav only.
- **SDK docs / codegen** — footer "SDKs" stays a placeholder/"coming soon".
- **Status page & Changelog** — footer "Status"/"Changelog" stay placeholders.
- **Versioned docs** — a single "latest" version only; no version switcher.
- Documenting the **private REST API** (`schema.yml`) — that is the internal client contract, not a public surface.
- Authoring a **CMS / admin editor** for docs content — content is sourced from code/markdown and the backend, not editable in-app.

## 2. Guiding Decisions

| Decision                                               | Resolution                                                                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Docs tooling**                                       | **Homegrown markdown**, not Fumadocs/Nextra. Reuse and extend the server-only `unified`/`remark`/`rehype` pipeline in [render-markdown.ts](../src/lib/render-markdown.ts). _Why:_ zero new heavy framework dependency; the app already renders sanitized markdown this way ([policy-document-view.tsx](../src/components/policy/policy-document-view.tsx)); full control over design-system styling.       |
| **Route placement**                                    | New `src/app/docs/**` route, **public, no auth wrapper**, sibling to `privacy`/`terms` — NOT under `(app)`. _Why:_ docs must render for anonymous prospects linked from marketing.                                                                                                                                                                                                                         |
| **Schema reference source**                            | **Live introspection at build time** against `NEXT_PUBLIC_API_BASE_URL/graphql/`, rendered to reference pages. _Why:_ always current, no manual SDL refresh step. Mitigated by the snapshot fallback below.                                                                                                                                                                                                |
| **Introspection resiliency**                           | Build fetches introspection and **writes a committed snapshot** (`src/lib/docs/__generated__/graphql-schema.json`); if the live fetch fails at build, fall back to the last committed snapshot and emit a build warning. _Why:_ the backend is _"not yet deployed — dev only"_; a docs deploy must never hard-fail on an unreachable backend. Honors the "live" choice while keeping builds deterministic. |
| **Concept-guide source**                               | **Fetched from the backend** over a new docs-serving endpoint (built in `~/Workspaces/vinta-schedule`), sourcing `docs/concepts/*.md`. Fetched at **build time (SSG)** with ISR revalidation, snapshot fallback as above. _Why:_ backend stays the single source of truth for domain docs; no duplicated markdown drifting in two repos.                                                                   |
| **Link rewriting**                                     | Concept markdown links point at backend-relative source paths (e.g. `../../calendar_integration/models.py`). A rehype pass **rewrites in-docs links** to `/docs/...` and **strips/neutralizes** links to backend source files. _Why:_ raw backend paths 404 in the frontend.                                                                                                                               |
| **Explorer**                                           | **Embedded GraphiQL** (open-source, self-hosted) on a dedicated `/docs/explorer` page, with a field to paste an `Authorization: Bearer <system_user_id>:<token>` header. _Why:_ lighter than Apollo Sandbox, no third-party hosted assets, matches the real auth header format in [middlewares.py:27](../../vinta-schedule/public_api/middlewares.py).                                                     |
| **Auth header format (documented + used by explorer)** | `Authorization: Bearer <system_user_id>:<token>` — the exact format the backend middleware parses. Tokens are minted at the in-app [api-tokens page](<../src/app/(app)/api-tokens/page.tsx>).                                                                                                                                                                                                              |
| **Feature flag**                                       | **None.** This is a purely additive new public surface (new route, new content endpoint). The only edits to existing code turn dead `href='#'` links into real routes — no existing flow changes shape. _(See Risk & Rollout Notes for the justification against the plan-feature default.)_                                                                                                               |
| **Nav**                                                | Static, config-driven sidebar (a typed `DocsNav` array) — no dynamic discovery. _Why:_ small, predictable, reviewable; search is a non-goal.                                                                                                                                                                                                                                                               |
| **Syntax highlighting**                                | Add a build-safe highlighter (e.g. `rehype-pretty-code`/Shiki, or `rehype-highlight`) to the markdown pipeline for code blocks. _Why:_ GraphQL/JSON/curl samples must be legible; the current pipeline emits plain `<code>`.                                                                                                                                                                               |

## 3. Data Model Changes

**Frontend:** none — no persistence. All content is fetched or generated at build time; nav is a static config module.

**Backend (`~/Workspaces/vinta-schedule`):** none — the new docs endpoint (Phase 2b) reads existing `docs/concepts/*.md` files from disk; no new tables or columns.

### 3.1 Type plumbing (frontend)

New TypeScript types under `src/lib/docs/`:

- `DocsNavItem` / `DocsNavSection` — the static sidebar config shape (title, slug, href, children).
- `GraphQLSchemaModel` — normalized shape parsed from the introspection JSON (types, queries, mutations, fields, args, descriptions) that the reference pages render.
- `ConceptDoc` — `{ slug: string; title: string; markdown: string }` returned by the backend docs endpoint.

## 4. API Design

### 4.1 Backend docs-serving endpoint (new, Phase 2b — `~/Workspaces/vinta-schedule`)

Serves the concept markdown so the frontend can fetch it. Public, read-only, unauthenticated (docs are public).

- `GET /public-api-docs/` → `200` `[{ "slug": "calendar-groups", "title": "Calendar Groups, Slots, and Slot Selections" }, ...]` — the manifest, one entry per `docs/concepts/*.md`.
- `GET /public-api-docs/{slug}/` → `200` `{ "slug": "...", "title": "...", "markdown": "..." }` — the raw markdown for one concept doc. `404` on unknown slug.
- Errors: `404` unknown slug; `503` handled by the frontend snapshot fallback (build proceeds on last-good).

_Title_ is derived from the first `# ` heading of each file. The endpoint reads files from the repo's `docs/concepts/` directory (bounded, static allow-list — no arbitrary path access).

### 4.2 GraphQL introspection (existing, consumed at build time)

`POST /graphql/` with the standard introspection query. Strawberry has introspection **on by default** (no disable found in `public_api/schema.py`), so no backend change is required beyond CORS (Phase 1b). The build reads the result into `GraphQLSchemaModel`.

## 5. Phased Rollout

Ordering rationale: the two **cross-repo backend phases (1b, 2b)** gate the schema reference and concept guides, so they start **in parallel from the top**. In-repo work that needs no backend (docs shell, getting-started guide, landing links) proceeds independently and delivers visible value first.

---

### Phase 0 — Docs shell, routing, and nav scaffold

**Goal**: A public `/docs` route renders with the design-system chrome and a static sidebar; landing chrome/prose styles reused. No real content yet (placeholder "Overview" page).

**Feature flag**: none — purely additive new route.

Changes:

1. `src/app/docs/layout.tsx`: public layout (no `(app)` auth wrapper) — sidebar nav + main `Container width='prose'`, mirroring the [privacy page](../src/app/privacy/page.tsx) shell.
2. `src/app/docs/page.tsx`: docs landing/overview page (static intro copy + links into the four sections).
3. `src/lib/docs/nav.ts`: the static `DocsNav` config (Getting Started, Schema Reference, Concepts, Webhooks, Explorer).
4. `src/components/docs/docs-sidebar.tsx`: renders `DocsNav`, marks the active item (usePathname).
5. `src/components/docs/docs-prose.tsx`: the prose wrapper + descendant styles for injected markdown HTML (extract the pattern already inline in [policy-document-view.tsx](../src/components/policy/policy-document-view.tsx)).

Use-case: shared scaffolding — no content use-case yet.

Tests:

- **Unit/component**: `src/components/docs/docs-sidebar.test.tsx` — renders all nav sections, highlights the active route.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step to `claude-sonnet-4-6` — touches 5 files + layout wiring). Pattern application against the privacy/terms precedent.

**Reusable skills**: `new-page` (App Router route scaffold); `add-storybook-story` (for `docs-sidebar`).

Acceptance: navigating to `/docs` as an anonymous user renders the sidebar and an overview page; `pnpm build` + `pnpm test` green.

---

### Phase 1b — Backend: allow docs origin + confirm introspection _(cross-repo, parallel — `~/Workspaces/vinta-schedule`)_

**Goal**: The browser can call `/graphql/` (introspection + GraphiQL) from the docs origin without CORS failures.

**Feature flag**: none.

Changes:

1. Add the docs frontend origin(s) to `CORS_ALLOWED_ORIGINS` (env-driven at [settings/base.py:295](../../vinta-schedule/vinta_schedule_api/settings/base.py#L295)) — dev + deployed docs origins.
2. Confirm the `Authorization` header is allowed for the `/graphql/` route (extend `CORS_ALLOW_HEADERS` if the standard `authorization` default header isn't already covered).
3. Verify introspection is reachable on `/graphql/` (Strawberry default-on; add a regression test asserting the introspection query returns `__schema`).

Use-case: shared cross-repo enablement for the Schema Reference + Explorer use-cases.

Tests:

- **Integration** (`public_api/tests/`): assert an introspection `POST /graphql/` returns `200` with `__schema`, and that a cross-origin preflight from a configured docs origin echoes the origin.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini`. Settings + one integration test against existing fixtures.

**Reusable skills**: `write-tests` (backend pytest conventions).

**Deploy ordering**: must be **deployed before** Phase 2 (schema reference build introspects live) and Phase 5 (explorer) run against a real backend. In-repo Phases 0/1/6 do not depend on it.

Acceptance: a browser fetch of the introspection query from the docs origin succeeds; preflight passes; introspection test green.

---

### Phase 2b — Backend: concept-docs serving endpoint _(cross-repo, parallel — `~/Workspaces/vinta-schedule`)_

**Goal**: Expose `docs/concepts/*.md` over HTTP so the frontend can fetch concept guides.

**Feature flag**: none — new read-only public endpoint.

Changes:

1. New view + routes implementing `GET /public-api-docs/` (manifest) and `GET /public-api-docs/{slug}/` (single doc) per **API Design**. Reads from a bounded allow-list of `docs/concepts/` files; title from the first `# ` heading.
2. Register the route; no auth (public docs).

Use-case: cross-repo enablement for the Concept Guides use-case.

Tests:

- **Integration** (`public_api/tests/` or a `docs/` test module): manifest lists all concept files; single-doc returns markdown + title; unknown slug → `404`; path-traversal slug (`../settings`) → `404`/rejected.

**Suggested AI model**: Tier 2/3 — `claude-sonnet-4-6`. Small view + careful path-safety on the slug allow-list.

**Reusable skills**: `write-tests`.

**Deploy ordering**: must be **deployed before** Phase 3's build fetches concept docs. Independent of Phases 0/1/2/5/6.

Acceptance: `GET /public-api-docs/` and `/public-api-docs/calendar-groups/` return the expected shapes; traversal rejected; tests green.

---

### Phase 1 — Getting-started + auth guide

**Goal**: A reader can go from zero to a first authenticated GraphQL call — how to mint a public-API token and send `Authorization: Bearer <system_user_id>:<token>`.

**Feature flag**: none.

Changes:

1. `src/app/docs/getting-started/page.tsx` + local markdown content (in-repo, authored) covering: what the API is, minting a token at the in-app [api-tokens page](<../src/app/(app)/api-tokens/page.tsx>) (link to it), the exact `Authorization` header format, and a first `curl`/GraphQL example (reusing the landing-page `calendarGroupBookableSlots` sample as the canonical first call).
2. Add the syntax-highlighting rehype plugin to a docs variant of the markdown pipeline (`src/lib/docs/render-doc-markdown.ts`, extending [render-markdown.ts](../src/lib/render-markdown.ts)); keep `rehype-sanitize` in the chain.
3. Add "Getting Started" to `DocsNav`.

Use-case: **Getting-started + auth guide**.

Tests:

- **Unit**: `src/lib/docs/render-doc-markdown.test.ts` — sanitized output, fenced code block gets highlighted markup, `javascript:` URL still stripped (sanitize still runs).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5`. Content page + one pipeline extension with clear precedent.

**Reusable skills**: `new-page`.

Acceptance: `/docs/getting-started` renders the auth guide with highlighted code and a working link to the api-tokens page; sanitize test proves no XSS regression.

---

### Phase 2 — GraphQL schema reference from live introspection

**Goal**: Auto-generated reference pages for the public GraphQL types, queries, and mutations, current with the deployed schema.

**Feature flag**: none.

Changes:

1. `src/lib/docs/introspect-schema.ts`: build-time fetch of the introspection query from `NEXT_PUBLIC_API_BASE_URL/graphql/`; on success, write `src/lib/docs/__generated__/graphql-schema.json`; on failure, read the last committed snapshot and warn (per **Guiding Decisions → Introspection resiliency**).
2. `src/lib/docs/parse-schema.ts`: normalize introspection JSON → `GraphQLSchemaModel`.
3. `src/app/docs/reference/[[...slug]]/page.tsx`: render an index of queries/mutations/types + a per-type detail view (fields, args, descriptions). SSG via `generateStaticParams` from the model.
4. Add "Schema Reference" (with generated sub-nav) to `DocsNav`.

Use-case: **GraphQL schema reference**.

Tests:

- **Unit**: `src/lib/docs/parse-schema.test.ts` — against a fixture introspection JSON, asserts queries (`calendarGroupBookableSlots`), mutations (`createCalendarGroupEvent`), and a type with nested fields/args are extracted.
- **Unit**: `introspect-schema.test.ts` — live-fetch failure falls back to the committed snapshot without throwing.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Introspection normalization + dynamic route generation + fallback logic across several files.

**Reusable skills**: `new-page`.

Acceptance: `/docs/reference` lists the real queries/mutations; a type page renders fields from introspection; with the backend unreachable at build, the page still builds from the snapshot.

---

### Phase 3 — Concept guides fetched from backend

**Goal**: Render the backend concept docs (Calendar Groups, Events, Availability, Recurrence, Calendar Bundles, Calendars) inside `/docs`, with backend-relative links rewritten.

**Feature flag**: none.

Changes:

1. `src/lib/docs/fetch-concepts.ts`: build-time fetch of the manifest + each doc from the Phase 2b endpoint; snapshot fallback to `src/lib/docs/__generated__/concepts.json` on failure.
2. Rehype link-rewrite pass in the docs markdown pipeline: in-docs concept links → `/docs/concepts/<slug>`; links to backend source files → neutralized/stripped.
3. `src/app/docs/concepts/[slug]/page.tsx`: SSG via `generateStaticParams` from the manifest; render markdown through the docs pipeline.
4. Add "Concepts" (with per-doc sub-nav) to `DocsNav`.

Use-case: **Concept guides**.

Tests:

- **Unit**: `src/lib/docs/rewrite-links.test.ts` — a `../../calendar_integration/models.py` link is neutralized; a `calendar-bundles.md` cross-link becomes `/docs/concepts/calendar-bundles`.
- **Unit**: `fetch-concepts.test.ts` — endpoint-down falls back to snapshot.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Fetch + rewrite + dynamic route + fallback.

**Reusable skills**: `new-page`.

Acceptance: `/docs/concepts/calendar-groups` renders the ported guide with working in-docs links and no dead backend-source links; build survives backend downtime via snapshot.

---

### Phase 4 — Webhooks reference

**Goal**: Document the outbound webhook event catalog and the GraphQL webhook configuration types.

**Feature flag**: none.

Changes:

1. `src/app/docs/webhooks/page.tsx` + content: enumerate the seven `WebhookEventType` values (`calendar_event_created/updated/deleted`, `..._attendee_added/removed/updated`, `organization_member_created`) with descriptions, and reference the `WebhookConfiguration` / `WebhookEvent` GraphQL types (link into the Phase 2 schema reference). Source of truth: the backend [webhooks/constants.py](../../vinta-schedule/webhooks/constants.py) + [webhooks/graphql.py](../../vinta-schedule/webhooks/graphql.py).
2. Add "Webhooks" to `DocsNav`.

Use-case: **Webhooks reference**.

Tests:

- **Unit/component**: `webhooks page` test — all seven event types render.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5`. Mostly authored content + a list render.

**Reusable skills**: `new-page`.

> Note: the event list is a fixed enum authored in-repo for v1. If it should stay auto-synced with the backend later, extend the Phase 2b endpoint to also serve the enum — tracked in **Open Questions**.

Acceptance: `/docs/webhooks` lists all seven events with descriptions and links to the relevant GraphQL types.

---

### Phase 5 — Embedded GraphiQL explorer

**Goal**: A live "try it" console at `/docs/explorer` that runs queries against `/graphql/` with a user-supplied token header.

**Feature flag**: none.

Changes:

1. Add GraphiQL as a client dependency; `src/components/docs/graphql-explorer.tsx` — a client component embedding GraphiQL, pointed at `NEXT_PUBLIC_API_BASE_URL/graphql/`, with a field to paste the `Authorization: Bearer <system_user_id>:<token>` header that GraphiQL sends on each request.
2. `src/app/docs/explorer/page.tsx`: hosts the explorer with a short "paste your token" preamble linking to the api-tokens page.
3. Add "Explorer" to `DocsNav`.

Use-case: **Live GraphiQL explorer**.

Tests:

- **Component**: `graphql-explorer.test.tsx` — renders the explorer, the header/token input updates the request headers passed to the fetcher (mock the fetcher; do not hit a network).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6`. Third-party embed + custom fetcher header wiring + SSR/client boundary care.

**Reusable skills**: None.

**Depends on**: Phase 1b deployed for real cross-origin calls (works locally against dev backend regardless).

Acceptance: `/docs/explorer` mounts GraphiQL; pasting a token sets the `Authorization` header on outgoing requests; a query against a running backend returns data.

---

### Phase 6 — Wire landing-page links to `/docs`

**Goal**: The marketing landing page's dead `href='#'` API-docs placeholders resolve to the new docs.

**Feature flag**: none.

Changes:

1. [marketing-home.tsx:408](../src/components/home-page/marketing-home.tsx#L408): "Read the API docs" button → `/docs` (or `/docs/getting-started`).
2. Footer "Developers" column ([marketing-home.tsx:906](../src/components/home-page/marketing-home.tsx#L906)): `API docs → /docs`, `Webhooks → /docs/webhooks`; leave `SDKs`, `Status`, `Changelog` as explicit "coming soon"/placeholder per non-goals.
3. Footer "Product → Booking API" → `/docs/reference` (or the getting-started guide).

Use-case: **Landing-page wiring**.

Tests:

- **Component**: update the marketing-home test (if present) to assert the API-docs links resolve to `/docs*` rather than `#`.

**Suggested AI model**: Tier 1 — `claude-haiku-4-5` / `gpt-5-nano`. Mechanical link edits.

**Reusable skills**: .

Acceptance: clicking the landing-page API-docs entry points lands on `/docs`; no API-docs link on the landing page still points at `#`.

## 6. Risk & Rollout Notes

- **No feature flag — justification.** The plan-feature default leans toward a flag when existing flows change. Here the surface is purely additive: new `/docs/**` routes, a new backend read-only endpoint, and new nav config that nothing existing reads. The only existing-code edits (Phase 6) replace dead `#` anchors with real hrefs — no behavior branch, no data path, no shared-table write. A flag would gate a static marketing link, which is not worth the debt. If Phase 6 is deemed risky, it can simply be the last-merged phase (ship docs first, flip links last) — an ordering lever, not a flag.
- **Build-time backend dependency (primary risk).** Schema reference (Phase 2) and concept guides (Phase 3) fetch live at build. The backend is _not yet deployed_. Mitigation: every live fetch writes a committed `__generated__` snapshot and falls back to it on failure, emitting a build warning — a docs deploy never hard-fails. Keep the snapshots committed so a cold clone builds offline.
- **Cross-repo deploy ordering.** Phase 1b (CORS + introspection) must be deployed before Phase 2/5 run against a real backend; Phase 2b (docs endpoint) before Phase 3. In-repo Phases 0/1/4/6 have no backend dependency and can ship first. Track the two backend PRs separately.
- **CORS with credentials.** The backend sets `CORS_ALLOW_CREDENTIALS=True`, so `Access-Control-Allow-Origin: *` is rejected — the docs origin must be added explicitly to `CORS_ALLOWED_ORIGINS` (Phase 1b). Verify the deployed docs origin (Vercel domain) is in the env value, not just localhost.
- **Sanitization.** The docs pipeline keeps `rehype-sanitize`. Concept markdown is backend-authored (semi-trusted) but still sanitized; the syntax-highlight plugin must run in a sanitize-compatible order (highlight → sanitize with a schema that permits the highlighter's `span`/`className`, or highlight after sanitize on already-safe nodes). A unit test asserts `javascript:` URLs are still stripped after adding highlighting.
- **Token safety in the explorer.** The GraphiQL token field holds a live public-API token in the browser. Do not persist it to localStorage by default; document that it's session-only, and that tokens are org-admin-scoped. Consider a visible "clear token" affordance.
- **Rollback.** Each phase is independently revertible: docs routes and the backend endpoint are additive (delete to remove); Phase 6 reverts to `#`. No migrations, no data changes.

## 7. Open Questions

| Question                                                                                                                      | Recommended default                                                                                                            | Owner          |
| ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------ | -------------- |
| What is the **deployed docs origin** (Vercel domain) to add to `CORS_ALLOWED_ORIGINS`?                                        | Add both the production Vercel domain and preview-deploy wildcard pattern if supported; otherwise prod + localhost.            | Eng (infra)    |
| Should the **webhook event list** stay hand-authored (Phase 4) or be served by the backend to auto-sync?                      | Hand-authored for v1 (the enum changes rarely); revisit if it drifts.                                                          | Product/Eng    |
| **ISR revalidation window** for concept docs fetched at build — pure SSG (rebuild to update) or ISR (e.g. revalidate hourly)? | Pure SSG for v1 (docs change with releases); add ISR only if content cadence demands it.                                       | Eng            |
| Does the deployed backend keep **introspection enabled in production**? (Some teams disable it publicly.)                     | Keep it on — it powers the reference + explorer; if security wants it off, switch Phase 2 to the committed-snapshot-only path. | Eng (security) |
| Preferred **highlighter** — `rehype-pretty-code`/Shiki (themable, heavier build) vs `rehype-highlight` (lighter)?             | `rehype-highlight` for v1 (smaller footprint); upgrade to Shiki if design wants token-accurate themes.                         | Eng            |

## 8. Touch List

**Phase 0 — docs shell** (frontend, new unless noted)

- `@src/app/docs/layout.tsx`, `@src/app/docs/page.tsx`
- `@src/lib/docs/nav.ts`
- `@src/components/docs/docs-sidebar.tsx`, `@src/components/docs/docs-prose.tsx`
- `@src/components/docs/docs-sidebar.test.tsx`

**Phase 1b — backend CORS/introspection** (`~/Workspaces/vinta-schedule`)

- edit `@vinta_schedule_api/settings/base.py` (CORS origins/headers)
- `@public_api/tests/` (introspection + preflight test)

**Phase 2b — backend docs endpoint** (`~/Workspaces/vinta-schedule`)

- new view + routes serving `docs/concepts/*.md` (e.g. `@public_api/` or a `docs/` module)
- backend test module for the endpoint

**Phase 1 — getting-started guide** (frontend)

- `@src/app/docs/getting-started/page.tsx` + local markdown content
- `@src/lib/docs/render-doc-markdown.ts`, `@src/lib/docs/render-doc-markdown.test.ts`
- edit `@src/lib/docs/nav.ts`

**Phase 2 — schema reference** (frontend)

- `@src/lib/docs/introspect-schema.ts`, `@src/lib/docs/parse-schema.ts`
- `@src/lib/docs/__generated__/graphql-schema.json` (committed snapshot)
- `@src/app/docs/reference/[[...slug]]/page.tsx`
- `@src/lib/docs/parse-schema.test.ts`, `@src/lib/docs/introspect-schema.test.ts`
- edit `@src/lib/docs/nav.ts`

**Phase 3 — concept guides** (frontend)

- `@src/lib/docs/fetch-concepts.ts`, `@src/lib/docs/rewrite-links.ts`
- `@src/lib/docs/__generated__/concepts.json` (committed snapshot)
- `@src/app/docs/concepts/[slug]/page.tsx`
- `@src/lib/docs/rewrite-links.test.ts`, `@src/lib/docs/fetch-concepts.test.ts`
- edit `@src/lib/docs/nav.ts`

**Phase 4 — webhooks reference** (frontend)

- `@src/app/docs/webhooks/page.tsx` + content
- webhooks page test
- edit `@src/lib/docs/nav.ts`

**Phase 5 — explorer** (frontend)

- edit `@package.json` (add GraphiQL)
- `@src/components/docs/graphql-explorer.tsx`, `@src/app/docs/explorer/page.tsx`
- `@src/components/docs/graphql-explorer.test.tsx`
- edit `@src/lib/docs/nav.ts`

**Phase 6 — landing links** (frontend)

- edit [marketing-home.tsx](../src/components/home-page/marketing-home.tsx) (button + footer hrefs)
- edit marketing-home test (if present)
