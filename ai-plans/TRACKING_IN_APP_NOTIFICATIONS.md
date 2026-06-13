# Tracking — IN_APP_NOTIFICATIONS

- **Feature**: In-App Notifications (vintasend)
- **Plan**: ai-plans/2026-06-13-IN_APP_NOTIFICATIONS_IMPLEMENTATION_PLAN.md
- **Started**: 2026-06-13
- **Last updated**: 2026-06-13
- **Feature flag**: none (purely additive surface)

## Run options
- pause_between_phases: false (auto-flow)
- generate_inline_comments: true
- use_worktree: true
- commit_strategy_resolved: modular-commits
- plan_branch: plan/in-app-notifications
- worktree_path: /Users/hugobessa/Workspaces/vinta-schedule/.claude/worktrees/plan-in-app-notifications
- worktree_branch: plan/in-app-notifications
- worktree_summary: .vinta-ai-workflows/worktrees/plan-in-app-notifications.yaml
- pr_url: https://github.com/vintasoftware/vinta-schedule-api/pull/67

## Completed phases

### Phase 0 — In-app adapter, renderer & DI wiring ✅
- **Model used**: claude-sonnet-4-6 (plan tier: 3)
- **Commits**: `0cbe17b` renderer, `e0ccf58` adapter, `0c88915` DI register, `592dfe4` example context, `b77a0fb` review fixes (trim override + docstring + TypeVar + assertion), `600269b` DI unread guard test
- **Review**: Layer-3 adversarial, 0 blockers; 3 should-fixes + 2 nits applied in-phase.
- **Summary**: Added `DjangoInAppNotificationAdapter` + `DjangoTemplatedInAppRenderer` (repo-local; vintasend_django ships neither). Registered IN_APP adapter in `di_core/containers.py`. Added `FixedDjangoDbNotificationBackend` — a one-method subclass fixing a real vendored bug where the unread queryset filtered by the raw `NotificationTypes.IN_APP` enum (serialized to `"NotificationTypes.IN_APP"` ≠ stored `"IN_APP"`); service-level `notification_backend` swapped to it. Example `in_app_generic_context` registered via `apps.ready()` + `templates/notifications/in_app/example.body.txt`. No migration, no flag.
- **Key fact for later phases**: `send()` does NOT persist a rendered body — vintasend stores the raw `context_used` dict; read paths must re-render `body_template` against `context_used`.
- **Gates**: notifications scoped 9 passed; full suite 1559 passed + 1 pre-existing unrelated SMS failure.

### Phase 1 — List unread endpoint ✅
- **Model used**: claude-sonnet-4-6 (plan tier: 2, straddled to 3 for passthrough pagination)
- **Commits**: `7e46c79` serializer, `9460822` unread endpoint, `b018fcc` drop premature list route (BLOCKER fix), `73be793` body-content + page_size-clamp tests
- **Review**: Layer-3, 1 BLOCKER (ListModelMixin shipped an unfiltered `GET /notifications/` route — leaked PENDING/FAILED/CANCELLED; fixed → `GenericViewSet` only) + 2 should-fixes (page_size clamp to 100; assert rendered body content) + 1 nit, all applied.
- **Summary**: `GET /notifications/unread/` via native `get_in_app_unread`, user-scoped, IsAuthenticated. Plain `NotificationSerializer` (works for dataclass + ORM model); `body` re-rendered at read time; `created`/`modified` None for dataclasses. Passthrough envelope `{results, page, page_size}`, page_size clamped to 100. Routes wired in `vinta_schedule_api/urls.py`; `schema.yml` regenerated.
- **Key facts for later phases**: `NotificationSerializer` reusable for Phase 2's ORM list. `get_queryset` already returns `Notification.objects.filter(user=request.user, notification_type=IN_APP).order_by("-created")` — Phase 2 adds `ListModelMixin` + `status__in=[SENT, READ]`. Tests auth via `APIClient().force_authenticate(user=user)`.
- **Gates**: notifications scoped 42 passed; full suite 1592 passed + 1 pre-existing unrelated SMS failure.

### Phase 2 — List all endpoint ✅
- **Model used**: claude-haiku-4-5 (plan tier: 2)
- **Commits**: `1e5c1f0` list-all endpoint, `d5d529b` deterministic ordering + stronger tests
- **Review**: Layer-3, 0 blockers + 3 should-fixes (added `-id` order tiebreaker; replaced tautological assertions; added mixed-status single-request test) + 1 nit (stale docstring), all applied.
- **Summary**: `GET /notifications/` lists user's IN_APP notifications with `status in (SENT, READ)` (excludes PENDING_SEND/FAILED/CANCELLED), newest-first (`-created, -id`), LimitOffsetPagination `{count, next, previous, results}`. Re-added `ListModelMixin`; `get_queryset` now status-filtered; `unread` action untouched. Reuses `NotificationSerializer`. schema.yml regenerated.
- **Note**: Branch rebased onto main's Twilio/SMS test fix; full suite now 0-failure.
- **Phase-3 coupling**: `get_queryset` excludes pipeline states. Phase 3 may reuse it for the mark-read lookup — a SENT row is findable (mark→READ), a READ row is findable (idempotent 200), PENDING/FAILED/CANCELLED → 404 (acceptable). Phase 3 must NOT add its own status filter.
- **Gates**: notifications scoped 63 passed; full suite 1615 passed, 0 failures.

## Current phase
- Phase 3 — Mark-as-read endpoint (Tier 2) — NEXT

## Remaining phases
- Phase 3 — Mark-as-read endpoint (Tier 2)

## Deferred phases
_(none — no cross-repo, no flag-removal)_
