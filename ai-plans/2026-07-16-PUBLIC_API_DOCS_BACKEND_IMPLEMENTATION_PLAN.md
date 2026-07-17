# Public API Docs — Backend Support — Implementation Plan

> No `..._SPEC.md` sibling exists. The requirements for this plan come from the **frontend** plan at
> `~/Workspaces/vinta-schedule-frontend-web/ai-plans/2026-07-16-PUBLIC_API_DOCS_IMPLEMENTATION_PLAN.md`,
> which specifies the docs site this backend work enables. That document's **Phase 1b** and **Phase 2b**
> are the cross-repo phases realized here (as Phases 1 and 2); Phase 3 below is new, from the planning
> session on 2026-07-16. Read the frontend plan's **API Design** section before implementing Phase 2.

## 1. Goals

1. **Let the docs site's browser code call `/graphql/`** from the deployed docs origins without CORS failures, so the embedded GraphiQL explorer and any browser-side introspection work in production.
2. **Serve the repo's concept documentation over HTTP** (`docs/concepts/*.md`) so the frontend can render it at build time, keeping this repo the single source of truth for domain docs rather than duplicating markdown into the frontend.
3. **Serve the webhook event catalog** — value, label, and a real human description per `WebhookEventType` member — so the published webhook reference can never drift from the enum.

**Non-goals:**

- **Authenticating the docs endpoints.** Public documentation is public; these are unauthenticated read-only reads.
- **A CMS or admin editor** for docs content. Content is files on disk in this repo, edited via PRs.
- **Serving arbitrary repo files.** Only a bounded allow-list under `docs/concepts/` and the webhook enum are exposed.
- **Rendering markdown to HTML server-side.** The endpoint returns raw markdown; the frontend owns rendering and sanitization.
- **Supporting Vercel preview-deploy origins in CORS.** Production, staging, and localhost only (see **Guiding Decisions**).
- **Versioned or historical docs.** Latest-on-`main` only.
- **Disabling introspection in production.** Explicitly decided against (see **Guiding Decisions**).

## 2. Guiding Decisions

| Decision | Resolution |
| --- | --- |
| **CORS origins** | Add `https://schedule.vintasoftware.com` (production) and `https://schedule-staging.vintasoftware.com` (staging) to the **`CORS_ALLOWED_ORIGINS` env var** on Render. `localhost:3000` is already present locally. _Why:_ [settings/base.py:295](@vinta_schedule_api/settings/base.py) already reads this via `config(..., cast=Csv())`, so this is a **deploy-time env change, not a code change**. The docs live at `/docs` on the same origin as the app, so no new origin class is introduced. |
| **No preview-deploy CORS** | No `CORS_ALLOWED_ORIGIN_REGEXES` setting is added. _Why:_ Vercel preview URLs are per-commit, so allowing them needs a regex — and `CORS_ALLOW_CREDENTIALS = True` means a loose pattern is a genuine security hole (any origin matching it can make credentialed cross-origin requests). The explorer working on prod, staging, and localhost is enough for v1. |
| **`authorization` header** | **No change needed.** `CORS_ALLOW_HEADERS` spreads `corsheaders.defaults.default_headers`, which already contains `authorization` (verified: `('accept', 'authorization', 'content-type', 'user-agent', 'x-csrftoken', 'x-requested-with')`). The frontend plan's Phase 1b speculated this might need extending; it does not. Phase 1 adds a regression test so it stays true. |
| **Introspection in production** | **Stays enabled.** _Why:_ this is a public, documented API — the schema is not a secret, and introspection powers both the docs schema reference and GraphiQL's autocomplete for real users. Strawberry enables it by default and no disable exists in [public_api/schema.py](@public_api/schema.py). Phase 1 adds a regression test so a future Strawberry upgrade or config change can't silently remove it. |
| **Endpoint placement** | The existing **`public_api/` app**, via its established `routes.py` → `RouteDict` → DRF-router pattern. _Why:_ no new app registration, reuses the app's view/serializer/test conventions, and the endpoint is conceptually part of the public API surface. The router registers `regex` as the URL prefix (see [public_api/routes.py](@public_api/routes.py)), so `public-api-docs` yields exactly the paths the frontend plan specifies. |
| **Closest precedent to copy** | [legal/views.py](@legal/views.py)'s `PolicyDocumentViewSet` — a **public, `AllowAny`, read-only document-serving viewset** registered through the same `RouteDict` pattern ([legal/routes.py](@legal/routes.py)). It is the nearest existing shape to what Phase 2 builds; read it before writing anything. Note its docstring explains why it deliberately does **not** build on the `*VintaScheduleModelViewSet` family (those mix in tenant scoping and virtual-model machinery that don't apply). The same reasoning applies here, and more so — the docs endpoints have no model at all. |
| **OpenAPI schema exposure** | These endpoints **will appear in the generated `schema.yml`** (drf-spectacular is in use — see the `@extend_schema` decorators in [legal/views.py](@legal/views.py)). Tag them `Docs` for grouping. _Why it matters:_ `schema.yml` is synced into the frontend repo and used to regenerate its typed client, so shipping these endpoints changes that file. See **Risk & Rollout Notes → Schema regeneration**. |
| **Content source** | Read `docs/concepts/*.md` **from disk at request time** against a bounded allow-list. _Why:_ the files ship with the deploy; no DB, no cache, no build step. Six files, small, read rarely (build-time fetches only). |
| **Title derivation** | Each doc's title is its **first `# ` heading**, parsed from the file. _Why:_ the frontend plan specifies it, and it keeps the title in one place — the document itself. |
| **Webhook descriptions live in this repo** | `WebhookEventType` carries only value + label, and the labels are title-cased restatements of the values (`calendar_event_created` → `"Calendar Event Created"`) — they carry no information. Phase 3 adds an explicit **description per member** next to the enum and serves it. _Why:_ the frontend must publish what each event means; if descriptions live there while the enum lives here, they drift, and a newly-added event renders blank. A test asserts every member has a description, so a new event cannot ship undocumented. |
| **Raw markdown, not HTML** | The endpoint returns the markdown string verbatim. _Why:_ the frontend already owns a sanitizing + syntax-highlighting markdown pipeline; returning HTML would duplicate that and force this repo to own XSS-sanitization for someone else's renderer. |
| **Feature flag** | **None.** All three phases are purely additive: new URL prefixes nothing existing reads, a new description mapping nothing existing imports, and an env-var value change. No existing flow changes shape, no shared table is written, no query plan moves. Per the skill's "purely additive new surface" exemption. |

## 3. Data Model Changes

**None.** No new tables, no new columns, no migrations. Phase 2 reads files from disk; Phase 3 adds a module-level mapping in Python next to an existing `TextChoices` enum.

### 3.1 Type plumbing

- `ConceptDocSummary` — `{ "slug": str, "title": str }`, the manifest entry shape.
- `ConceptDoc` — `{ "slug": str, "title": str, "markdown": str }`, the single-doc shape. Mirrors the frontend's `ConceptDoc` type exactly (frontend plan, Data Model Changes 3.1).
- `WebhookEventDoc` — `{ "value": str, "label": str, "description": str }`.

These are DRF serializers over plain dicts, not models — there is nothing to persist.

## 4. API Design

All endpoints are **public, unauthenticated, read-only** (`GET` only), registered under the `public-api-docs` prefix in [public_api/routes.py](@public_api/routes.py).

### 4.1 Concept docs

- `GET /public-api-docs/` → `200`
  ```json
  [{ "slug": "calendar-groups", "title": "Calendar Groups, Slots, and Slot Selections" }, ...]
  ```
  One entry per file in the `docs/concepts/` allow-list. Title from the file's first `# ` heading.

- `GET /public-api-docs/{slug}/` → `200`
  ```json
  { "slug": "calendar-groups", "title": "Calendar Groups, ...", "markdown": "# Calendar Groups...\n" }
  ```
  - `404` — unknown slug, **including any slug not in the allow-list**. Path-traversal attempts (`../settings`, `..%2Fsettings`, absolute paths) resolve to `404`, never a file read.
  - `503` is not raised by this endpoint; the frontend's snapshot fallback covers the endpoint being unreachable.

The six documents that exist today: `availability`, `calendar-bundles`, `calendar-groups`, `calendars`, `events`, `recurrence`.

### 4.2 Webhook event catalog

- `GET /public-api-docs/webhook-events/` → `200`
  ```json
  [{ "value": "calendar_event_created", "label": "Calendar Event Created",
     "description": "Fires when a calendar event is created in a calendar your token can access. ..." }, ...]
  ```
  One entry per `WebhookEventType` member, in enum declaration order.

**Routing trap — read before implementing.** `webhook-events` sits at the same level as `{slug}`, so on a DRF `ViewSet` this must be an `@action(detail=False, url_path="webhook-events")`, which the router registers **before** the detail route. That resolves correctly, but it means **`webhook-events` is a reserved slug**: a concept doc named `webhook-events.md` would be shadowed and unreachable. Phase 3 adds a test asserting the reserved name is not also a concept slug, so the collision can't appear silently later.

## 5. Phased Rollout

Ordering rationale: Phase 1 unblocks the frontend's explorer and any live-schema build; Phase 2 unblocks the frontend's concept guides; Phase 3 unblocks (and changes) the frontend's webhook reference. The three are **independent of each other** and can land in any order — they are sequenced by which frontend phase is waiting.

---

### Phase 1 — Lock in CORS and introspection for the docs origin

**Goal**: The docs origin can call `/graphql/` from a browser, and introspection answers — both guaranteed by tests rather than by luck.

**Feature flag**: none — no behavior change; see **Guiding Decisions**.

Changes:

1. **No settings code change.** The production/staging origins are added to the **`CORS_ALLOWED_ORIGINS` env var on Render** — see **Risk & Rollout Notes → Deploy steps**. `CORS_ALLOW_HEADERS` already permits `authorization` via `default_headers`; do not touch it.
2. `@public_api/tests/test_docs_cors_and_introspection.py`: new test module locking in the three properties the frontend depends on. This is the whole deliverable of this phase — the point is that a future settings refactor, a `corsheaders` upgrade that changes `default_headers`, or a Strawberry upgrade that flips introspection off must fail CI here rather than silently break the published docs.
3. `@.env.example`: document the production/staging origins so a new developer's env matches deployed reality. The file exists and currently carries `CORS_ALLOWED_ORIGINS=http://localhost:3000,http://localhost:8000` — extend the comment, keep the localhost default (a developer's local value should stay localhost-only).

Spec use-case: shared cross-repo enablement for the frontend's **Schema Reference** and **Live GraphiQL explorer** use-cases.

Tests:

- **Integration** `@public_api/tests/test_docs_cors_and_introspection.py`:
  - `POST /graphql/` with the standard introspection query returns `200` and a body containing `data.__schema` with a non-empty `types` list. Guards the **Guiding Decisions → Introspection in production** choice.
  - A cross-origin **preflight** (`OPTIONS /graphql/` with `Origin` + `Access-Control-Request-Method: POST` + `Access-Control-Request-Headers: authorization`) from an origin in `CORS_ALLOWED_ORIGINS` echoes that origin in `Access-Control-Allow-Origin` and lists `authorization` in `Access-Control-Allow-Headers`. Override the setting in-test (`django.test.override_settings`) rather than depending on the ambient env value.
  - A preflight from an origin **not** in `CORS_ALLOWED_ORIGINS` does **not** echo it. This is the test that actually has teeth — it fails if someone ever sets a wildcard, which `CORS_ALLOW_CREDENTIALS = True` makes unsafe.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../ai-tools/skills/plan-feature/resources/ai-models.yaml)). Integration tests against established fixtures, no production code. The only subtlety is driving `corsheaders` preflight behavior correctly from the test client.

**Reusable skills**: none. (`add-env-var` does **not** apply — `CORS_ALLOWED_ORIGINS` already exists and is already read; only its deployed *value* changes.)

**Deploy ordering**: the env change must be applied on Render **before** the frontend's explorer (its Phase 5) is exercised in production. The frontend's schema reference does not block on it — it falls back to a committed snapshot.

Acceptance: `uv run pytest public_api/tests/test_docs_cors_and_introspection.py` passes; an introspection `POST /graphql/` returns `__schema`; a preflight from a configured origin echoes it and permits `authorization`; a preflight from an unconfigured origin does not.

---

### Phase 2 — Serve concept docs over HTTP

**Goal**: A client can list the concept documents and fetch any one of them as raw markdown.

**Feature flag**: none — brand-new URL prefix nothing existing reads.

Changes:

1. `@public_api/docs_content.py` (new): the content layer, kept separate from the view so it is unit-testable without HTTP.
   - A module-level **allow-list** derived by globbing `docs/concepts/*.md` **once at import**, mapping `slug` → resolved absolute `Path`. Slug is the filename stem (`calendar-groups.md` → `calendar-groups`).
   - `list_concept_docs()` → `list[ConceptDocSummary]`, sorted deterministically (alphabetical by slug).
   - `get_concept_doc(slug)` → `ConceptDoc`, raising a not-found error for any slug not a **key of the allow-list**.
   - `_extract_title(markdown)` → the first `# ` heading's text; fall back to a title-cased slug if a file has no heading (do not raise — a doc without a heading should still be listed).
   - **Resolve the concepts directory from `settings.BASE_DIR`**, not a relative path from `__file__`, so it behaves identically under pytest, runserver, and the Render deploy. Note `BASE_DIR` is a **`str`**, not a `Path` ([settings/base.py:10](@vinta_schedule_api/settings/base.py)) — wrap it: `Path(settings.BASE_DIR) / "docs" / "concepts"`.
   - **The lookup is a dict key check, not a path join.** Never build a filesystem path from the request slug. This is what makes traversal structurally impossible rather than filtered.
2. `@public_api/serializers.py`: `ConceptDocSummarySerializer`, `ConceptDocSerializer` — plain `Serializer` subclasses over dicts, not `ModelSerializer`.
3. `@public_api/views.py`: `PublicApiDocsViewSet(ViewSet)` with `list()` and `retrieve()`, `lookup_field = "slug"`, `lookup_value_regex = "[a-z0-9-]+"`, `permission_classes = [AllowAny]`, `authentication_classes = []`, and `@extend_schema(tags=["Docs"])`. **Model it on [legal/views.py](@legal/views.py)'s `PolicyDocumentViewSet`** — same public/read-only/`AllowAny` shape, same `RouteDict` registration. Use a plain `ViewSet` (not `ReadOnlyModelViewSet`): there is no model or queryset here, only dicts.
4. `@public_api/routes.py`: register `{"regex": r"public-api-docs", "viewset": PublicApiDocsViewSet, "basename": "PublicAPIDocs"}`.

Spec use-case: cross-repo enablement for the frontend's **Concept guides** use-case.

Tests:

- **Unit** `@public_api/tests/test_docs_content.py`:
  - `list_concept_docs()` returns all six real files with titles taken from their real first headings.
  - `_extract_title` handles: a normal `# Title`, a file whose first heading is not on line 1, and a file with no heading at all (fallback, no raise).
  - `get_concept_doc("calendar-groups")` returns markdown that is byte-identical to the file on disk.
- **Integration** `@public_api/tests/test_docs_endpoint.py`:
  - `GET /public-api-docs/` → `200`, lists all six, shape matches **API Design 4.1**.
  - `GET /public-api-docs/calendar-groups/` → `200` with `slug`/`title`/`markdown`.
  - `GET /public-api-docs/does-not-exist/` → `404`.
  - **Traversal**: `../settings`, `..%2F..%2Fsettings`, `%2Fetc%2Fpasswd`, and an absolute path all return `404`/`400` and **never** read a file outside `docs/concepts/`. Parametrize these.
  - **Unauthenticated access succeeds** — assert explicitly with no credentials, since every other route in this app requires auth and a future global default-permission change must fail here.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../ai-tools/skills/plan-feature/resources/ai-models.yaml)). Small surface, but the path-safety design and the "dict lookup, never path join" property are the whole point of the phase and deserve care; step down to Tier 2 only if the implementer is following `create-rest-endpoint` closely.

**Reusable skills**: `create-rest-endpoint` — read `ai-tools/skills/create-rest-endpoint/SKILL.md` first and follow its viewset/serializer/routes conventions.

**Deploy ordering**: must be deployed before the frontend's Phase 3 build fetches concept docs. The frontend can still land its phase without it — it falls back to a committed snapshot — but the docs will be stale until this ships.

Acceptance: `GET /public-api-docs/` returns the six documents with real titles; `GET /public-api-docs/calendar-groups/` returns that file's markdown verbatim; every traversal payload in the parametrized test returns `404`; all of it works with no credentials.

---

### Phase 3 — Serve the webhook event catalog

**Goal**: A client can fetch every webhook event type with a real description of what it means, sourced from this repo.

**Feature flag**: none — additive action on a new viewset; nothing existing reads it.

Changes:

1. `@webhooks/constants.py`: add `WEBHOOK_EVENT_DESCRIPTIONS: dict[WebhookEventType, str]` next to `WebhookEventType`, with a one-to-three-sentence description per member covering **what triggers it** and **what it carries**. Author these against the real dispatch sites — grep for where each event is emitted rather than paraphrasing the label. The seven members today: `CALENDAR_EVENT_CREATED`, `CALENDAR_EVENT_UPDATED`, `CALENDAR_EVENT_DELETED`, `CALENDAR_EVENT_ATTENDEE_ADDED`, `CALENDAR_EVENT_ATTENDEE_REMOVED`, `CALENDAR_EVENT_ATTENDEE_UPDATED`, `ORGANIZATION_MEMBER_CREATED`.
2. `@public_api/serializers.py`: `WebhookEventDocSerializer` (`value`, `label`, `description`).
3. `@public_api/views.py`: add `@action(detail=False, methods=["get"], url_path="webhook-events")` to `PublicApiDocsViewSet`, returning one entry per enum member in declaration order. **See the routing trap in API Design 4.2** — this must be `detail=False` so the router places it before the `{slug}` detail route.

Spec use-case: cross-repo enablement for the frontend's **Webhooks reference** use-case.

Tests:

- **Unit** `@webhooks/tests/test_constants.py`:
  - **Every `WebhookEventType` member has a non-empty description.** This is the load-bearing test: it makes it impossible to add an event without documenting it. Iterate the enum, not a hardcoded list — a test over a hardcoded list of seven would pass for an eighth undocumented event.
  - `WEBHOOK_EVENT_DESCRIPTIONS` has no keys that are not enum members (guards against a rename leaving an orphan).
- **Integration** `@public_api/tests/test_docs_endpoint.py` (extend):
  - `GET /public-api-docs/webhook-events/` → `200`, one entry per enum member, each with a non-empty `description`, in declaration order.
  - Unauthenticated access succeeds.
  - **`webhook-events` does not collide with a concept slug** — assert `"webhook-events"` is not a key of the concept allow-list, so the reserved-name shadowing described in **API Design 4.2** cannot appear silently if someone later adds `docs/concepts/webhook-events.md`.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../ai-tools/skills/plan-feature/resources/ai-models.yaml)). A mapping, a serializer, and one router action against clear precedent. The descriptions themselves need care but are prose, not architecture.

**Reusable skills**: `create-rest-endpoint` for the action wiring.

**Deploy ordering**: must be deployed before the frontend's Phase 4 consumes it. **The frontend plan currently hand-authors this list** — see **Risk & Rollout Notes → Cross-repo consequence**.

Acceptance: `GET /public-api-docs/webhook-events/` returns all seven events with non-empty descriptions; adding a new `WebhookEventType` member without a description fails `uv run pytest webhooks/tests/test_constants.py`.

## 6. Risk & Rollout Notes

- **No feature flag — justification.** All three phases are purely additive: a new URL prefix nothing existing reads, a new dict nothing existing imports, and an env-var value change. The plan-feature default leans toward a flag when existing flows change shape; none do here. No migration, no shared-table write, no query-plan movement. Rollback is `git revert` plus (for Phase 1) restoring the previous env value.
- **Deploy steps (Phase 1, manual).** On Render, for both the production and staging services, append to `CORS_ALLOWED_ORIGINS`:
  - production: `https://schedule.vintasoftware.com`
  - staging: `https://schedule-staging.vintasoftware.com`
  The var is CSV (`cast=Csv()`), so this is a value edit, not a code deploy. **Verify the deployed value after editing** — a typo here fails open into a broken explorer, not a loud error, and CORS failures only appear in the browser console.
- **CORS with credentials.** `CORS_ALLOW_CREDENTIALS = True` means `Access-Control-Allow-Origin: *` is rejected by browsers and must never be configured. Every allowed origin is explicit. This is why preview-deploy support was declined rather than solved with a permissive regex — see **Guiding Decisions**.
- **Path traversal is the main security risk in Phase 2.** The mitigation is structural, not a filter: slugs are looked up as **keys in an allow-list dict** built by globbing at import, so a request string is never joined into a filesystem path. Reviewers should reject any implementation that does `Path(concepts_dir) / f"{slug}.md"` — even with validation, that shape is one refactor away from a traversal.
- **Reading files at request time.** Six small files, read only by build-time fetches from the frontend. No cache is added. If read volume ever grows (public traffic, ISR with a short window), revisit — but do not pre-optimize now.
- **Cross-repo consequence — the frontend plan needs amending.** The frontend's **Phase 4** currently hand-authors the seven webhook event types, and its **Open Questions** records "hand-authored for v1" as the recommended default. That decision was **reversed** in this plan's Step 0: the backend now owns the catalog and its descriptions. After Phase 3 ships, run `amend-plan` against the frontend plan to rewrite its Phase 4 to fetch `/public-api-docs/webhook-events/` (with the same snapshot-fallback shape its Phases 2/3 use) instead of hard-coding the list.
- **Schema regeneration (Phases 2 and 3).** drf-spectacular picks up the new endpoints, so the committed `schema.yml` changes. Regenerate it as part of each phase with `make update_schema` (`python manage.py spectacular --color --file schema.yml`) and commit the result, so the tracked schema matches the code. Knock-on effect: that file is synced into the frontend repo and used to regenerate its typed client, so these phases produce a **frontend-side codegen diff even though the frontend needs no hand-written change for them** — it fetches the docs endpoints with a plain build-time `fetch`, not the generated client. Expect new `PublicAPIDocs*` operations to appear in the frontend's `src/client/` on its next sync; nothing consumes them, and that is fine.
- **Introspection is now a tested contract.** Phase 1's test means a future decision to disable introspection must consciously delete a test rather than quietly break the published schema reference and the explorer's autocomplete. That is the intent.
- **Rollback.** Each phase reverts independently: Phases 2 and 3 delete cleanly (additive routes); Phase 1 is an env value plus a test module.

## 7. Open Questions

| Question | Recommended default | Owner |
| --- | --- | --- |
| Should the concept-docs endpoint be cached (ETag / `Cache-Control`) once the frontend moves off pure SSG to ISR? | No cache for v1 — build-time reads only, six small files. Add `ETag` if ISR with a short revalidate window lands. | Eng |
| Should `docs/concepts/` gain a lint/CI check that every file has an `# ` heading? | Not for v1 — `_extract_title` falls back to a title-cased slug rather than raising, so a missing heading degrades gracefully. Add a check if titles start looking wrong in the published docs. | Eng |
| Do preview-deploy origins ever need CORS (e.g. QA reviewing the explorer on a preview URL)? | No for v1 — decided in planning. If QA needs it, add `CORS_ALLOWED_ORIGIN_REGEXES` with a **tightly anchored** pattern and treat it as a security review, given `CORS_ALLOW_CREDENTIALS = True`. | Eng (infra) / Security |
| Should other `docs/` subtrees (beyond `concepts/`) be exposed later? | No — the allow-list is deliberately one directory. Widening it is a new decision, not a config tweak. | Product / Eng |

## 8. Touch List

**Phase 1 — CORS + introspection** (this repo, plus a manual deploy step)

- new `@public_api/tests/test_docs_cors_and_introspection.py`
- **no code change** to `@vinta_schedule_api/settings/base.py` — the origins are an env-var value edit on Render
- edit `@.env.example` (only if the file exists)

**Phase 2 — concept-docs endpoint** (this repo)

- new `@public_api/docs_content.py`
- edit `@public_api/serializers.py` (`ConceptDocSummarySerializer`, `ConceptDocSerializer`)
- edit `@public_api/views.py` (`PublicApiDocsViewSet`)
- edit `@public_api/routes.py` (register `public-api-docs`)
- new `@public_api/tests/test_docs_content.py`, `@public_api/tests/test_docs_endpoint.py`
- regenerate + commit `@schema.yml` (`make update_schema`)
- reads (does not modify) `@docs/concepts/*.md`
- read-only precedent to follow: `@legal/views.py`, `@legal/routes.py`

**Phase 3 — webhook event catalog** (this repo)

- edit `@webhooks/constants.py` (`WEBHOOK_EVENT_DESCRIPTIONS`)
- edit `@public_api/serializers.py` (`WebhookEventDocSerializer`)
- edit `@public_api/views.py` (`webhook-events` action on `PublicApiDocsViewSet`)
- new `@webhooks/tests/test_constants.py`
- edit `@public_api/tests/test_docs_endpoint.py`
- regenerate + commit `@schema.yml` (`make update_schema`)

**Cross-repo (frontend, `~/Workspaces/vinta-schedule-frontend-web`)**

- after Phase 3: `amend-plan` the frontend's `ai-plans/2026-07-16-PUBLIC_API_DOCS_IMPLEMENTATION_PLAN.md` Phase 4 to consume `/public-api-docs/webhook-events/` instead of hand-authoring the list.
