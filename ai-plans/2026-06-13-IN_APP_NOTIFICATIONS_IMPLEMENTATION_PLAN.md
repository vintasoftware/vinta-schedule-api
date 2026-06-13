# In-App Notifications (vintasend) — Implementation Plan

## 1. Goals

1. Make in-app notifications **sendable** through vintasend's native `NotificationService.create_notification(..., notification_type=IN_APP)` by registering a real IN_APP adapter + renderer in the DI container (today `get_in_app_unread` and any IN_APP send raises `NotificationError("No in-app notification adapter found")`).
2. Expose an internal REST endpoint to **list all** of the current user's in-app notifications (read + unread).
3. Expose an internal REST endpoint to **list only unread** in-app notifications, backed by vintasend's native `get_in_app_unread`.
4. Expose an internal REST endpoint to **mark a single notification as read**, backed by vintasend's native `mark_read`, with ownership enforcement so a user can only mark their own notifications.

**Non-goals:**
- Wiring in-app notifications into any existing flow (org invitations, calendar events, etc.). v1 ships the *capability* + a reusable example context only — no triggers in existing code paths.
- Bulk "mark all as read" (vintasend has no native bulk method; deferred).
- Real-time delivery (websockets / SSE / push). The IN_APP adapter persists + renders only; clients poll the list endpoints.
- Public GraphQL surface. Internal REST only.
- Email / SMS / PUSH changes. Those adapters stay exactly as configured.
- A new database table or migration. Reuses the existing `vintasend_django` `Notification` model.
- Editing, deleting, or "unread again" transitions on notifications.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Use native vintasend methods** | List-unread → `NotificationService.get_in_app_unread(user_id, page, page_size)`; mark-read → `NotificationService.mark_read(notification_id)`; sending → `NotificationService.create_notification(...)`. These are the project's contract per the request; we do not reimplement them. |
| **"List all" has no native method** | vintasend only exposes unread (`get_in_app_unread` / backend `filter_*_in_app_unread_notifications`). For "list all" we add a thin ORM query on the existing `vintasend_django` `Notification` model filtered to `user=request.user, notification_type=IN_APP` (statuses `SENT` + `READ`). Documented as a deliberate gap-fill, still reusing the vendored model — no new table. |
| **IN_APP adapter is required infra** | The DI container ([di_core/containers.py](../di_core/containers.py)) registers only Email + SMS adapters. Without an IN_APP adapter, both sending and `get_in_app_unread` raise. We add a minimal repo-local `DjangoInAppNotificationAdapter` (`notification_type = IN_APP`) plus an in-app template renderer, since vintasend_django ships neither. The adapter renders the body template on `send`; persistence + status transitions stay with `DjangoDbNotificationBackend`. |
| **User-scoped, not org-scoped** | Notifications belong to a `User` (the model's `user` FK), independent of organization. Endpoints filter by `request.user`; no `OrganizationModel` / membership scoping. |
| **Ownership enforcement on mark-read** | Native `mark_read(notification_id)` takes only an id — no user check. The viewset first loads the notification, 404s if it isn't `request.user`'s IN_APP notification, then calls `mark_read`. Prevents IDOR. |
| **Pagination split** | "All" endpoint returns an ORM queryset → standard project `LimitOffsetPagination` (PAGE_SIZE 10). "Unread" endpoint calls native `get_in_app_unread`, which takes `page`/`page_size` and returns an `Iterable` (not a queryset) → passthrough pagination: accept `page`/`page_size` query params, hand them to the native method, serialize the returned page. Documented so the two list endpoints' query params differ intentionally. |
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

### 4.1 List all notifications

- **GET** `/notifications/` → `200`
- Query params: `limit`, `offset` (standard `LimitOffsetPagination`).
- Returns the user's IN_APP notifications with `status in (SENT, READ)`, newest first.
- Response item: `{ id, title, notification_type, status, body, created, modified }` (`body` = rendered body when available via `context_used`, else the template-rendered body; see Phase 2 note). Paginated envelope: `{ count, next, previous, results: [...] }`.

### 4.2 List unread notifications

- **GET** `/notifications/unread/` → `200`
- Query params: `page` (default 1), `page_size` (default 10) — passthrough to native `get_in_app_unread`.
- Returns only `status=SENT` IN_APP notifications for the user via `NotificationService.get_in_app_unread(request.user.id, page, page_size)`.
- Response: list of the same item shape as the "all" endpoint (passthrough pagination — envelope documented in the phase; no `count` from native, so `next` is "has more" heuristic based on page fill).

### 4.3 Mark a notification as read

- **POST** `/notifications/{id}/mark-read/` → `200` with the updated notification, or `404` if the id is not an IN_APP notification owned by `request.user`.
- Calls `NotificationService.mark_read(id)` after the ownership check.
- Idempotent: marking an already-`READ` notification returns `200` with unchanged state (no error).

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
2. `notifications/views.py` (new): `NotificationViewSet` (read viewset). Inject `notification_service` via `@inject` + `Provide["notification_service"]` (same constructor pattern as [organizations/views.py](../organizations/views.py)). Add an `unread` action (`GET /notifications/unread/`) reading `page`/`page_size` query params, calling `self.notification_service.get_in_app_unread(request.user.id, page, page_size)`, serializing the returned iterable with passthrough pagination envelope.
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

### Phase 2 — List all endpoint (thin ORM, read + unread)

**Goal**: Authenticated user can `GET /notifications/` and see all their in-app notifications (read + unread).

**Feature flag**: none — additive new endpoint.

Changes:
1. `notifications/views.py`: implement the `NotificationViewSet` `list`/`get_queryset` to return `Notification.objects.filter(user=request.user, notification_type=IN_APP, status__in=[SENT, READ]).order_by("-created")`. Standard `LimitOffsetPagination` (project default). Reuse the Phase 1 `NotificationSerializer`.
2. `notifications/serializers.py`: ensure `body` resolution handles `context_used`-rendered body vs. unrendered fallback (document chosen source).

Spec use-case: List all notifications (Goal 2).

Tests:
- **Integration**: `notifications/tests/test_list_endpoint.py` — seed SENT + READ + PENDING_SEND + FAILED for the user and rows for another user; assert `GET /notifications/` returns only the user's SENT+READ rows newest-first, excludes PENDING/FAILED, respects `limit`/`offset`, isolates by user, 401 when unauth.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. ORM-backed list mirroring existing read viewsets.

**Reusable skills**: `create-rest-endpoint`; `write-tests`.

Acceptance: `GET /notifications/` returns the current user's `IN_APP` notifications with `status in (SENT, READ)`, newest first, paginated via `limit`/`offset`, isolated per user.

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
| Should `body` in the response be the **rendered** body (from `context_used` after send) or the raw template path? | Return the rendered body when `context_used` is present; fall back to rendering `body_template` with `context_kwargs`. Confirm the frontend's expectation. | Frontend + backend |
| Do we need an **unread count** endpoint (badge) for the UI? | Defer to a follow-up; derivable from `/notifications/unread/` length for now. | Product |
| Index on `(user, notification_type, status)` for the "all" query at scale? | Defer until list latency is measured in prod; add a migration only if needed. | Backend |
| Bulk **mark-all-read** in a later version? | Deferred (Non-goal). Revisit if the UI needs it; would loop native `mark_read` or a custom bulk ORM update. | Product |

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

**Phase 2 — list all endpoint**
- @notifications/views.py (edit — `get_queryset`/`list`)
- @notifications/serializers.py (edit — `body` resolution)
- @notifications/tests/test_list_endpoint.py (new)

**Phase 3 — mark-as-read endpoint**
- @notifications/views.py (edit — `mark_read` detail action)
- @notifications/tests/test_mark_read_endpoint.py (new)
