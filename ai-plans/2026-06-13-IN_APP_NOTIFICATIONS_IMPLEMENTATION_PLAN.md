# In-App Notifications (vintasend) — Implementation Plan

> **Addendum (2026-06-14) — reconciled to vintasend / vintasend-django 1.2.0.**
> The first cut (Phases 0–3) shipped against vintasend 1.1.3, which lacked a native
> "list all" method and had an IN_APP enum-filter bug, forcing two workarounds: a
> repo-local `FixedDjangoDbNotificationBackend` and a thin ORM query for the "all" list.
> **1.2.0 fixes both** and adds native bulk mark-read + count methods. This plan is now
> updated to: (a) drop the `FixedDjangoDbNotificationBackend` workaround (use the stock
> `DjangoDbNotificationBackend`); (b) back the "list all" endpoint with native
> `get_in_app_notifications` + `get_in_app_notifications_count`; (c) add `count` to the
> unread envelope via `get_in_app_unread_count`; (d) add a **bulk mark-read** endpoint
> via native `mark_read_bulk` (Phase 4). In 1.2.0 the vintasend `Notification` dataclass
> also carries `created`, `modified`, and `context_used`, so the serializer's
> timestamp/body fields now resolve for natively-returned notifications, not just ORM rows.

## 1. Goals

1. Make in-app notifications **sendable** through vintasend's native `NotificationService.create_notification(..., notification_type=IN_APP)` by registering a real IN_APP adapter + renderer in the DI container (today `get_in_app_unread` and any IN_APP send raises `NotificationError("No in-app notification adapter found")`).
2. Expose an internal REST endpoint to **list all** of the current user's in-app notifications (read + unread), backed by vintasend's native `get_in_app_notifications` + `get_in_app_notifications_count` (1.2.0).
3. Expose an internal REST endpoint to **list only unread** in-app notifications, backed by vintasend's native `get_in_app_unread` (+ `get_in_app_unread_count` for the total).
4. Expose an internal REST endpoint to **mark a single notification as read**, backed by vintasend's native `mark_read`, with ownership enforcement so a user can only mark their own notifications.
5. Expose an internal REST endpoint to **mark multiple notifications as read at once**, backed by vintasend's native `mark_read_bulk(ids, user_id=...)` — ownership-scoped + idempotent (1.2.0).

**Non-goals:**
- Wiring in-app notifications into any existing flow (org invitations, calendar events, etc.). v1 ships the *capability* + a reusable example context only — no triggers in existing code paths.
- "Mark ALL unread as read" in a single sweep (no-id-list mark-everything). Bulk is by explicit id list only; a mark-everything sweep is a separate follow-up.
- Real-time delivery (websockets / SSE / push). The IN_APP adapter persists + renders only; clients poll the list endpoints.
- Public GraphQL surface. Internal REST only.
- Email / SMS / PUSH changes. Those adapters stay exactly as configured.
- A new database table or migration. Reuses the existing `vintasend_django` `Notification` model.
- Editing, deleting, or "unread again" transitions on notifications.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **vintasend ≥ 1.2.0** | Pins bumped to `vintasend>=1.2.0,<2`, `vintasend-django>=1.2.0,<2`, `vintasend-celery>=1.2.0,<2`. 1.2.0 fixes the IN_APP enum-filter bug, adds native `get_in_app_notifications` / `get_in_app_notifications_count` / `get_in_app_unread_count` / `mark_read_bulk`, and populates `created`/`modified`/`context_used` on the `Notification` dataclass. The earlier `FixedDjangoDbNotificationBackend` workaround is removed. |
| **Use native vintasend methods** | List-all → `get_in_app_notifications(user_id, page, page_size)` + `get_in_app_notifications_count(user_id)`; list-unread → `get_in_app_unread(user_id, page, page_size)` + `get_in_app_unread_count(user_id)`; mark-read → `mark_read(notification_id)`; bulk mark-read → `mark_read_bulk(ids, user_id=...)`; sending → `create_notification(...)`. All native; we do not reimplement them. |
| **No more "list all" workaround** | 1.1.3 had no native "all" method, so the first cut used a thin ORM query (`Notification.objects.filter(...)` via `get_queryset` + `ListModelMixin` + `LimitOffsetPagination`). 1.2.0 ships native `get_in_app_notifications` + count, so the list-all endpoint now uses the native method (passthrough `page`/`page_size` + a real `count`), consistent with the unread endpoint. The ORM `get_queryset` is retained **only** for the single mark-read `get_object()` ownership lookup, not for listing. |
| **IN_APP adapter is required infra** | The DI container ([di_core/containers.py](../di_core/containers.py)) registers only Email + SMS adapters. Without an IN_APP adapter, both sending and the in-app read methods raise. We add a minimal repo-local `DjangoInAppNotificationAdapter` (`notification_type = IN_APP`) plus an in-app template renderer, since vintasend_django ships neither. The adapter renders the body template on `send`; persistence + status transitions stay with the stock `DjangoDbNotificationBackend` (no subclass — the 1.1.3 enum bug that required `FixedDjangoDbNotificationBackend` is fixed upstream in 1.2.0). |
| **User-scoped, not org-scoped** | Notifications belong to a `User` (the model's `user` FK), independent of organization. Endpoints filter by `request.user`; no `OrganizationModel` / membership scoping. |
| **Ownership enforcement on mark-read** | Single mark-read: the viewset's `get_object()` (scoped queryset: `user=request.user`, `notification_type=IN_APP`, `status in SENT/READ`) 404s on a foreign/unknown id before any native call. Bulk mark-read: native `mark_read_bulk(ids, user_id=request.user.id)` scopes the update to the user server-side, so foreign ids in the list are silently never touched. Both prevent IDOR. |
| **Pagination — native + count everywhere** | BOTH list endpoints use native methods that take `page`/`page_size` and return an `Iterable` (not a queryset), serialized into a passthrough envelope `{ results, page, page_size, count }`. `count` comes from `get_in_app_notifications_count` / `get_in_app_unread_count`. `page_size` is clamped to a max (100). LimitOffsetPagination is no longer used for these endpoints — the two list endpoints now share one consistent contract. |
| **No feature flag** | Purely additive surface: brand-new REST endpoints + a new adapter/renderer no existing code path invokes. The only shared touch is appending one adapter to the DI `notification_adapters` list, which cannot change Email/SMS behavior. Per plan-feature rules, additive-only ⇒ no flag. |
| **App placement** | All new code lives in the existing `notifications` app (already holds the vintasend periodic send task). |
| **DI injection into the viewset** | The viewset receives `notification_service` via the project's `@inject` + `Annotated[NotificationService, Provide["notification_service"]]` constructor pattern (same as `OrganizationViewSet`). |

## 3. Data Model Changes

No schema changes. Reuses `vintasend_django.models.Notification`:

- `user` (FK → `users.User`), `notification_type` (`IN_APP`), `title`, `body_template`, `context_name`, `context_kwargs`, `status` (`PENDING_SEND` → `SENT` → `READ`), `created`, `modified`.
- "Unread" = `notification_type=IN_APP, status=SENT`. "Read" = `status=READ`. "All" = `status in (SENT, READ)` (excludes `PENDING_SEND`, `FAILED`, `CANCELLED` from the user-facing list).

### 3.1 Type plumbing

- No new TypedDicts/NewTypes required. Serializers map directly off the vendored model fields. The example context function returns a plain `dict` matching vintasend's `NotificationContextDict` shape (same pattern as [accounts/notification_contexts.py](../accounts/notification_contexts.py)).

## 4. API Design

All endpoints are internal REST, JWT/session auth, `IsAuthenticated`, user-scoped via `request.user`. Registered through a new `notifications/routes.py` aggregated in [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py). Base path group: `notifications`.

All list endpoints share one passthrough envelope: `{ results: [...], page, page_size, count }`. Response item: `{ id, title, notification_type, status, body, created, modified }`. `page_size` is clamped to a max of 100.

### 4.1 List all notifications

- **GET** `/notifications/` → `200`
- Query params: `page` (default 1), `page_size` (default 10, max 100).
- Returns the user's IN_APP notifications with `status in (SENT, READ)`, newest first, via `NotificationService.get_in_app_notifications(request.user.id, page, page_size)`; `count` from `get_in_app_notifications_count(request.user.id)`.

### 4.2 List unread notifications

- **GET** `/notifications/unread/` → `200`
- Query params: `page` (default 1), `page_size` (default 10, max 100).
- Returns only `status=SENT` IN_APP notifications via `NotificationService.get_in_app_unread(request.user.id, page, page_size)`; `count` from `get_in_app_unread_count(request.user.id)`.

### 4.3 Mark a notification as read

- **POST** `/notifications/{id}/mark-read/` → `200` with the updated notification, or `404` if the id is not an IN_APP notification owned by `request.user`.
- Calls `NotificationService.mark_read(id)` after the `get_object()` ownership check.
- Idempotent: marking an already-`READ` notification returns `200` with unchanged state (the viewset short-circuits and also catches `NotificationUpdateError` for the concurrent race).

### 4.4 Mark multiple notifications as read (bulk)

- **POST** `/notifications/mark-read-bulk/` (collection-level; distinct from the detail `…/{id}/mark-read/`) → `200`.
- Request body: `{ "ids": [<id>, ...] }` (non-empty list of notification ids; validated → `400` on empty/malformed).
- Calls `NotificationService.mark_read_bulk(ids, user_id=request.user.id)` — ownership-scoped (foreign ids silently skipped, never an error) and idempotent (already-READ/missing/non-SENT ids skipped).
- Response: `{ "results": [ ...serialized notifications that are READ after the op... ] }` (the requested ids the user owns and that are now READ; the count of returned items may be fewer than requested ids).
- Unauthenticated → `401`.

## 5. Phased Rollout

Ordered so the blocking infra (adapter/renderer + DI wiring) lands first; each endpoint use-case is then its own small, independently mergeable phase.

### Phase 0 — In-app adapter, renderer & DI wiring

**Goal**: Make `notification_type=IN_APP` a working channel so `create_notification(... IN_APP ...)` and `get_in_app_unread` stop raising. No user-visible endpoint yet.

**Feature flag**: none — pure additive infra; appends one adapter to the DI `notification_adapters` list, no existing channel changes.

Changes:
1. `notifications/notification_adapters/django_in_app.py` (new): `DjangoInAppNotificationAdapter(BaseNotificationAdapter)` with `notification_type = NotificationTypes.IN_APP`. `send()` renders the body template via the in-app renderer (validates render, populates context) and returns; persistence + status stay with `DjangoDbNotificationBackend`. Model on the vintasend `FakeInAppAdapter` + `DjangoEmailNotificationAdapter` shape.
2. `notifications/notification_template_renderers/django_in_app_renderer.py` (new): a minimal `DjangoTemplatedInAppRenderer(BaseNotificationTemplateRenderer)` that renders the `body_template` (and optionally `title`) with Django templates. Mirror `DjangoTemplatedEmailRenderer` but single-body (closer to the SMS renderer's single-template shape).
3. [di_core/containers.py](../di_core/containers.py): import the new adapter + renderer; append `DjangoInAppNotificationAdapter(DjangoTemplatedInAppRenderer(), DjangoDbNotificationBackend())` to the `notification_adapters=[...]` list of the `notification_service` Singleton. Leave Email/SMS entries untouched.
4. `notifications/notification_contexts.py` (new): one registered example context (e.g. `in_app_generic_context`) following [accounts/notification_contexts.py](../accounts/notification_contexts.py) registration style, so callers have a working precedent. Register it on app load (`apps.py` `ready()` or module import, matching how existing contexts register).
5. `templates/notifications/in_app/*.body.txt` (new): one example body template the context renders against.

Spec use-case: shared scaffolding — no use-case yet (unblocks Phases 1–3).

Tests:
- **Unit**: `notifications/tests/test_in_app_adapter.py` — `send()` renders the template without error; adapter advertises `notification_type == IN_APP`.
- **Integration**: `notifications/tests/test_in_app_send.py` — building a `NotificationService` with the in-app adapter (mirroring the DI wiring) + calling `create_notification(notification_type=IN_APP, ...)` persists a `Notification` and transitions it to `SENT`; `get_in_app_unread(user_id)` returns it and no longer raises `NotificationError`.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. New adapter + renderer implementing a vendored ABC, plus DI Singleton edit with generic type params — multi-file, exact-contract-sensitive.

**Reusable skills**: `write-tests` (integration test under the notifications tests dir).

Acceptance: with the new adapter registered, `NotificationService.create_notification(user_id=<u>, notification_type="in_app", ...)` persists a SENT IN_APP notification and `get_in_app_unread(<u>)` returns it — neither raises.

---

### Phase 1 — List unread endpoint (native `get_in_app_unread`)

**Goal**: Authenticated user can `GET /notifications/unread/` and see only their unread in-app notifications.

**Feature flag**: none — additive new endpoint.

Changes:
1. `notifications/serializers.py` (new): `NotificationSerializer` (read-only) exposing `id, title, notification_type, status, body, created, modified`.
2. `notifications/views.py` (new): `NotificationViewSet` (read viewset). Inject `notification_service` via `@inject` + `Provide["notification_service"]` (same constructor pattern as [organizations/views.py](../organizations/views.py)). Add an `unread` action (`GET /notifications/unread/`) reading `page`/`page_size` query params, calling `self.notification_service.get_in_app_unread(request.user.id, page, page_size)`, serializing the returned iterable into the passthrough envelope `{ results, page, page_size, count }` — `count` from `get_in_app_unread_count(request.user.id)` (added in 1.2.0; the first cut omitted `count`).
3. `notifications/routes.py` (new): register `NotificationViewSet` under regex `notifications`, basename `Notifications`.
4. [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py): import `notifications_routes` and add to the `routes` aggregation tuple.

Spec use-case: List only unread notifications (Goal 3).

Tests:
- **Unit**: `notifications/tests/test_serializers.py` — `NotificationSerializer` shape.
- **Integration**: `notifications/tests/test_unread_endpoint.py` — seed SENT (unread) + READ + another user's IN_APP notifications; assert `GET /notifications/unread/` returns only the requesting user's SENT items, respects `page`/`page_size`, requires auth (401 unauth), and never returns another user's rows.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step up to `claude-sonnet-4-6` if passthrough pagination needs iteration) / `gpt-5-mini` / `gemini-2.5-flash`. Serializer + viewset action mirroring existing precedent.

**Reusable skills**: `create-rest-endpoint` (viewset/serializer/route/schema wiring); `write-tests`.

Acceptance: `GET /notifications/unread/` returns only the current user's `IN_APP`+`SENT` notifications, paginated by `page`/`page_size`; unauthenticated requests get 401.

---

### Phase 2 — List all endpoint (native `get_in_app_notifications`, read + unread)

> **Updated for 1.2.0.** Originally specced as a thin ORM list (`get_queryset` + `ListModelMixin` + `LimitOffsetPagination`) because 1.1.3 had no native "all" method. 1.2.0 ships native `get_in_app_notifications` + `get_in_app_notifications_count`, so the endpoint now uses the native method with passthrough `page`/`page_size` + `count`, consistent with the unread endpoint.

**Goal**: Authenticated user can `GET /notifications/` and see all their in-app notifications (read + unread).

**Feature flag**: none — additive new endpoint.

Changes:
1. `notifications/views.py`: implement `list` (custom, not `ListModelMixin`) calling `self.notification_service.get_in_app_notifications(request.user.id, page, page_size)` for the page and `self.notification_service.get_in_app_notifications_count(request.user.id)` for the total; return the passthrough envelope `{ results, page, page_size, count }`. Read `page`/`page_size` (clamped to max 100) exactly like the `unread` action. Reuse the Phase 1 `NotificationSerializer`. Keep `get_queryset` (now only consumed by Phase 3's `get_object()` ownership lookup, not by listing).
2. `notifications/serializers.py`: no change needed — `body` re-renders `body_template` against `context_used` / `context_kwargs`, and in 1.2.0 the native dataclass carries `context_used` + `created` + `modified`, so the same serializer works identically for native results and ORM rows.

Spec use-case: List all notifications (Goal 2).

Tests:
- **Integration**: `notifications/tests/test_list_endpoint.py` — seed SENT + READ + PENDING_SEND + FAILED for the user (via the service / direct ORM for the excluded statuses) and rows for another user; assert `GET /notifications/` returns only the user's SENT+READ rows newest-first, excludes PENDING/FAILED/CANCELLED, respects `page`/`page_size`, returns the `{results, page, page_size, count}` envelope with a correct `count`, isolates by user, 401 when unauth.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Native-method-backed list action mirroring the `unread` action.

**Reusable skills**: `create-rest-endpoint`; `write-tests`.

Acceptance: `GET /notifications/` returns the current user's `IN_APP` notifications with `status in (SENT, READ)`, newest first, paginated via `page`/`page_size` with a real `count`, isolated per user.

---

### Phase 3 — Mark-as-read endpoint (native `mark_read`, ownership-enforced)

**Goal**: Authenticated user can `POST /notifications/{id}/mark-read/` to mark one of their notifications read.

**Feature flag**: none — additive new endpoint.

Changes:
1. `notifications/views.py`: add a detail action `mark_read` (`POST /notifications/{id}/mark-read/`). First fetch the notification scoped to `user=request.user, notification_type=IN_APP` (404 via `get_object_or_404`/queryset lookup if not found or not owned), then call `self.notification_service.mark_read(notification.id)`, return the serialized updated notification.
2. Confirm route already registered (Phase 1) — detail action needs no new `routes.py` entry.

Spec use-case: Mark a notification as read (Goal 4).

Tests:
- **Integration**: `notifications/tests/test_mark_read_endpoint.py` —
  - owner marks own SENT notification → `200`, status becomes `READ`, disappears from `/notifications/unread/`;
  - marking another user's notification → `404`, target unchanged (IDOR guard);
  - marking an already-`READ` notification → `200`, idempotent;
  - marking a non-existent id → `404`;
  - unauthenticated → `401`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single detail action + ownership check + native call.

**Reusable skills**: `create-rest-endpoint`; `write-tests`.

Acceptance: `POST /notifications/{id}/mark-read/` marks the owner's notification `READ` and removes it from the unread list; non-owners and unknown ids get `404`; repeat calls are idempotent.

---

### Phase 4 — Bulk mark-as-read endpoint (native `mark_read_bulk`)

> **Added in the 1.2.0 reconciliation (2026-06-14).** Possible only with 1.2.0's native `mark_read_bulk`.

**Goal**: Authenticated user can `POST /notifications/mark-read-bulk/` with a list of ids to mark several notifications read at once.

**Feature flag**: none — additive new endpoint.

Changes:
1. `notifications/serializers.py`: add a small input serializer `BulkMarkReadSerializer` with `ids = serializers.ListField(child=serializers.CharField(), allow_empty=False)` (validate non-empty list of ids → `400` on empty/malformed).
2. `notifications/views.py`: add a collection action `@action(detail=False, methods=["post"], url_path="mark-read-bulk")` named `mark_read_bulk`. A **distinct** `url_path` (`mark-read-bulk`, not `mark-read`) is deliberate: sharing `url_path="mark-read"` with the detail `mark_read` action makes drf-spectacular derive the same operationId for both → a collision warning + an order-dependent `_2` suffix. The distinct path yields stable, unique operationIds with no global schema hooks. Validate the body with `BulkMarkReadSerializer`, then call `self.notification_service.mark_read_bulk(ids, user_id=request.user.id)` (ownership-scoped → foreign ids silently skipped; idempotent). Serialize the returned iterable with `NotificationSerializer(many=True)` and return `{ "results": [...] }` at `200`.
3. Route already registered (Phase 1) — the collection action needs no new `routes.py` entry. Regenerate `schema.yml`.

Spec use-case: Mark multiple notifications as read (Goal 5).

Tests:
- **Integration**: `notifications/tests/test_bulk_mark_read_endpoint.py` —
  - user POSTs ids of two own SENT notifications → `200`; both become `READ` in the DB and drop out of `/notifications/unread/`;
  - mix of own SENT + own already-READ ids → `200`, idempotent (no error), all returned READ;
  - including another user's id in the list → that row is NOT marked (ownership scope), still SENT in the DB, and absent from the response (IDOR guard);
  - empty / missing `ids` → `400`;
  - non-existent ids in the list → silently skipped, `200`;
  - unauthenticated → `401`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Single collection action + input serializer + native call.

**Reusable skills**: `create-rest-endpoint`; `write-tests`.

Acceptance: `POST /notifications/mark-read-bulk/` with a list of ids marks the caller's SENT notifications `READ` (idempotent, ownership-scoped); foreign/unknown ids are silently skipped; empty/over-100 body → `400`; unauth → `401`.

---

### Phase R — Reconcile to vintasend 1.2.0 (dependency bump + drop workarounds)

> **Cross-cutting (2026-06-14).** Not a user-facing endpoint; the foundation the Phase 2/4 updates build on. Landed as its own commits ahead of the endpoint changes.

**Goal**: Adopt vintasend 1.2.0 and remove the 1.1.3 workarounds.

**Feature flag**: none.

Changes:
1. `pyproject.toml` + `uv.lock`: bump `vintasend` / `vintasend-django` / `vintasend-celery` to `>=1.2.0,<2`; `uv lock --upgrade-package …` + `uv sync`.
2. Delete `notifications/notification_backends.py` (`FixedDjangoDbNotificationBackend` — the enum bug it patched is fixed upstream).
3. `di_core/containers.py`: drop the `FixedDjangoDbNotificationBackend` import; the in-app adapter and the service-level `notification_backend` use the stock `DjangoDbNotificationBackend`.
4. `notifications/tests/test_in_app_send.py`: update `_build_notification_service` to use the stock `DjangoDbNotificationBackend`; keep the DI end-to-end unread test (now guards the stock wiring).

Tests: full suite green on 1.2.0; the in-app send/unread integration tests pass against the stock backend.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Dependency bump + delete-and-rewire.

**Reusable skills**: `write-tests`.

Acceptance: stock `DjangoDbNotificationBackend` is the only backend; `notification_backends.py` is gone; `grep -r FixedDjangoDbNotificationBackend` returns nothing; full suite green on 1.2.0.

## 6. Risk & Rollout Notes

- **No feature flag** — every phase is additive (new endpoints, new adapter/renderer). The single shared edit is appending one entry to the DI `notification_adapters` list; it introduces a new channel and cannot alter Email/SMS sending. Phase 0's integration test asserts existing channels still resolve. No flag-removal phase needed.
- **No migration / no locks** — reuses the existing `vintasend_django` `Notification` table. Zero DDL, zero rewrite, no hot-table risk.
- **DI Singleton edit** is the highest-blast-radius change: a malformed adapter entry could break *all* notification sending at process start. Mitigated by Phase 0's integration test constructing the service the same way the container does, and by landing Phase 0 alone before any endpoint depends on it.
- **Performance** — "all" endpoint queries `Notification` by `(user, notification_type, status)`. Confirm the vendored model has a usable index on `user`/`status`; if list latency regresses at scale, follow up with a covering index (out of scope for v1; note in Open Questions).
- **Ownership / IDOR** — native `mark_read` trusts the id; the viewset's owner-scoped fetch is the security boundary. Covered by an explicit cross-user 404 test in Phase 3.
- **Rollback** — revert the phase PR. Phase 0 revert removes the IN_APP adapter (reverts to pre-feature "no in-app channel"); endpoint phases revert independently. No data migration to undo.
- **Backfill** — none. Existing rows are untouched; the list endpoints simply surface whatever IN_APP notifications exist.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should `body` in the response be the **rendered** body or the raw template path? | **Resolved**: rendered body — `body_template` re-rendered against `context_used` (now on the 1.2.0 dataclass) with `context_kwargs` fallback. | Backend |
| **Unread count** for a UI badge? | **Resolved**: both list envelopes now include `count` (`get_in_app_unread_count` / `get_in_app_notifications_count`). No separate count-only endpoint unless the badge needs one without fetching a page. | Product |
| Index on `(user, notification_type, status)` for the list queries at scale? | Defer until list latency is measured in prod; the index would live in `vintasend_django`'s model (upstream), not this repo. | Backend |
| "Mark ALL unread as read" (no-id sweep)? | Deferred (Non-goal). The id-list bulk endpoint covers the immediate need; a sweep would use a future `mark_all_in_app_read(user_id)` if 1.x adds one. | Product |

## 8. Touch List

**Phase 0 — adapter / renderer / DI**
- @notifications/notification_adapters/django_in_app.py (new)
- @notifications/notification_template_renderers/django_in_app_renderer.py (new)
- @notifications/notification_contexts.py (new)
- @templates/notifications/in_app/example.body.txt (new)
- [di_core/containers.py](../di_core/containers.py) (edit — append IN_APP adapter)
- @notifications/tests/test_in_app_adapter.py (new)
- @notifications/tests/test_in_app_send.py (new)

**Phase 1 — unread endpoint**
- @notifications/serializers.py (new)
- @notifications/views.py (new)
- @notifications/routes.py (new)
- [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py) (edit — aggregate `notifications_routes`)
- @notifications/tests/test_serializers.py (new)
- @notifications/tests/test_unread_endpoint.py (new)

**Phase 2 — list all endpoint (native)**
- @notifications/views.py (edit — native `list` via `get_in_app_notifications` + count)
- @notifications/tests/test_list_endpoint.py (new)

**Phase 3 — mark-as-read endpoint**
- @notifications/views.py (edit — `mark_read` detail action)
- @notifications/tests/test_mark_read_endpoint.py (new)

**Phase 4 — bulk mark-as-read endpoint**
- @notifications/serializers.py (edit — `BulkMarkReadSerializer`)
- @notifications/views.py (edit — `mark_read_bulk` collection action)
- @notifications/tests/test_bulk_mark_read_endpoint.py (new)

**Phase R — reconcile to vintasend 1.2.0**
- [pyproject.toml](../pyproject.toml) (edit — bump vintasend* to `>=1.2.0`)
- [uv.lock](../uv.lock) (edit — re-lock)
- @notifications/notification_backends.py (delete — drop `FixedDjangoDbNotificationBackend`)
- [di_core/containers.py](../di_core/containers.py) (edit — stock `DjangoDbNotificationBackend`)
- @notifications/tests/test_in_app_send.py (edit — stock backend)
