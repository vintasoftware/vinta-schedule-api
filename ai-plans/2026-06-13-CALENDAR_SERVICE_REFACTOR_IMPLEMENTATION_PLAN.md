# Calendar Service Refactor — Implementation Plan

> Splitting the 4,726-line `CalendarService` monolith into a thin facade plus focused
> sub-services, with no observable behavior change. There is no `..._SPEC.md` sibling — this is a
> pure structural refactor driven directly by the request "split the gigantic calendar service file
> into more specific services without making performance significantly worse." Decisions were
> confirmed interactively (see **Guiding Decisions**).

## 1. Goals

1. Reduce [calendar_service.py](../calendar_integration/services/calendar_service.py) (4,726 lines, single `CalendarService` class) to a thin **facade** that owns authentication state and delegates each concern to a focused sub-service, each living in its own module under ~400–900 lines.
2. Extract five cohesive sub-services — `CalendarSyncService`, `CalendarEventService`, `CalendarBundleService`, `AvailabilityService`, `CalendarWebhookService` — plus a shared `RecurrenceManager` helper and a shared utils module, each independently readable and unit-testable.
3. Preserve behavior **byte-for-byte** at every public call site: views, serializers, GraphQL mutations, Celery tasks, `organizations/services.py`, `public_api/queries.py`, and `CalendarGroupService` continue to inject `CalendarService` and call the same methods with the same results.
4. Do not regress performance: authenticate once (one calendar-adapter construction / token refresh per request), share that auth context across sub-services — never re-authenticate per concern.
5. Fix the latent multi-tenant correctness bug in the `@lru_cache`-decorated calendar lookups (`_get_calendar_by_id` / `_get_calendar_by_external_id`) as part of the foundation work, with a regression test.

**Non-goals:**
- No change to the public method signatures, return types, or exceptions of `CalendarService`. (Internal `_private` helpers may move/rename freely.)
- No change to `CalendarGroupService`, `CalendarPermissionService`, or `CalendarSideEffectsService` beyond what delegation wiring requires (they already exist as separate services).
- No new features, endpoints, models, migrations, or DB schema changes.
- No rewrite of call sites to inject sub-services directly (facade is retained; a later "deprecate the facade" track is explicitly out of scope for this plan).
- No feature flag (pure refactor — see **Guiding Decisions**).
- No change to the calendar adapters (`google_calendar_adapter`, `ms_outlook_calendar_adapter`) or the recurrence math in [recurrence_utils.py](../calendar_integration/recurrence_utils.py).

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Public API shape** | Keep `CalendarService` as the injected **facade**. It retains every public method as a thin delegation. *Why:* zero blast radius on the ~8 call-site modules and the existing test suite; each extraction phase stays small and independently reversible. |
| **Auth-context sharing** | Facade authenticates **once** (`authenticate()` / `initialize_without_provider()`), builds an immutable `CalendarServiceContext` (organization, user_or_token, account, calendar_adapter, permission_service, side_effects_service), and passes it to each sub-service. *Why:* calendar-adapter construction performs an OAuth token refresh (network call); re-authenticating per sub-service would multiply that cost. This is the explicit perf guardrail. |
| **Sub-service construction** | Facade lazily constructs sub-services **after** authentication, injecting the shared context plus any sibling sub-service a concern needs (e.g. `CalendarBundleService` gets `CalendarEventService`). Sub-services are plain classes, not DI-container providers — only the facade stays in the container. *Why:* keeps DI wiring untouched (one provider), and sibling wiring is explicit and local to the facade. |
| **Recurrence machinery** | Extract the generic template-method engine (`_create_recurring_exception_generic`, `_create_recurring_bulk_modification_generic`) into a stateless `RecurrenceManager` helper that `CalendarEventService` and `AvailabilityService` delegate to. *Why:* the machinery is shared by events, blocked-times, and available-times; a stateless helper keeps it DRY without pulling mutation logic away from the entity services. Mirrors the existing `recurrence_utils.py` helper pattern. |
| **Availability + blocked-time grouping** | One `AvailabilityService` owns available-times **and** blocked-times. *Why:* `get_availability_windows_in_range` is literally "all time minus blocked-times minus events," so the two share interval math and recurrence expansion; splitting them would force a chatty cross-service dependency for every availability query. |
| **lru_cache bug** | Replace the instance-level `@lru_cache` on `_get_calendar_by_id` / `_get_calendar_by_external_id` with a per-context-instance dict cache (or remove caching) so a reused service instance can never return another organization's `Calendar`. *Why:* the cache key omits organization; across a reused instance this is a cross-tenant data-leak risk. The refactor already rewrites these methods, so the fix is in-scope and cheap. |
| **No feature flag** | Pure refactor with no behavior change; correctness is provable via the unchanged existing test suite + diff review. *Why:* the project's flag convention exempts behavior-preserving refactors. The lone correctness change (lru_cache fix) is guarded by a new regression test, not a flag. |
| **Behavior-preservation gate** | Every phase must leave the **existing** `calendar_integration` test suite (~33k LoC, incl. [test_calendar_service.py](../calendar_integration/tests/services/test_calendar_service.py)) green **without edits to those tests** (except where a test reaches into a now-moved `_private` symbol). *Why:* the existing suite is the contract; needing to rewrite assertions signals a behavior change. |

## 3. Data Model Changes

**None.** No models, fields, migrations, or SQL. The only new types are in-process plumbing:

### 3.1 `CalendarServiceContext` (new dataclass)

New file `@calendar_integration/services/calendar_service_context.py`. Frozen dataclass holding the shared auth/collaborator state the facade currently keeps as instance attributes:

```python
@dataclasses.dataclass(frozen=True)
class CalendarServiceContext:
    organization: Organization | None
    user_or_token: User | str | SystemUser | None
    account: SocialAccount | GoogleCalendarServiceAccount | None
    calendar_adapter: CalendarAdapter | None
    calendar_permission_service: CalendarPermissionService | None
    calendar_side_effects_service: CalendarSideEffectsService | None
```

The existing `is_authenticated_calendar_service` / `is_initialized_calendar_service` / `is_initialized_or_authenticated_calendar_service` type-guards in [type_guards.py](../calendar_integration/services/type_guards.py) already inspect `.organization` / `.account` / `.calendar_adapter` by attribute. The facade keeps those attributes (the guards keep working on the facade); sub-services read them from the context. Export the context from `@calendar_integration/services/__init__.py` only if referenced cross-module.

### 3.2 Shared helper modules (new, no public surface)

- `@calendar_integration/services/calendar_service_utils.py` — module-level functions moved verbatim from `CalendarService`: `convert_naive_utc_datetime_to_timezone`, the `_serialize_event*` family, calendar lookups (`get_calendar_by_id` / `get_calendar_by_external_id`, lru-bug fixed), and the permission-granting helpers (`grant_calendar_owner_permissions`, `grant_event_attendee_permissions`).
- `@calendar_integration/services/recurrence_manager.py` — `RecurrenceManager` wrapping the two generic engines.

## 4. API Design

No external API surface changes (no REST routes, no GraphQL schema, no serializers' public shape). Omitted intentionally — numbering continues.

## 5. Phased Rollout

Phases are ordered **dependency-foundational-first**: shared plumbing → the most-depended-on concern (events, via the recurrence helper) → concerns that consume it (bundle, availability, sync) → the leaf consumer (webhooks) → facade shrink. Each phase keeps the facade's public methods intact (delegating), so any phase merged alone leaves the system fully working, and any phase reverted alone restores the prior delegation.

> **All phases share these two Tests entries** (stated once, not repeated verbatim below):
> - **Regression gate**: full `calendar_integration` test suite green with **no edits** to existing test assertions (only import-path updates if a test referenced a moved `_private` symbol). Run: the project's pytest command for `calendar_integration/`.
> - **Delegation test**: a focused unit test asserting the facade method still returns the sub-service's result (e.g. monkeypatch the sub-service method, assert the facade forwards args + result).

### Phase 0 — Shared context, utils module, and lru_cache fix

**Goal**: Introduce `CalendarServiceContext` and the shared `calendar_service_utils` module; rewire the facade to build the context on authenticate and call the shared utils. Fix the multi-tenant `lru_cache` bug. No sub-services yet. Ship value: none on its own — required scaffolding every later phase consumes.

**Feature flag**: none — pure scaffolding, no reachable behavior change (except the lru_cache correctness fix, covered by a regression test).

Changes:
1. New `@calendar_integration/services/calendar_service_context.py`: the frozen dataclass (Data Model Changes).
2. New `@calendar_integration/services/calendar_service_utils.py`: move `convert_naive_utc_datetime_to_timezone`, `_serialize_event*` helpers, `_grant_calendar_owner_permissions` / `_grant_event_attendee_permissions` (as module functions taking the permission service), and the two calendar lookups. **Fix the lookups**: drop `@lru_cache` (lines 615–629 of [calendar_service.py](../calendar_integration/services/calendar_service.py#L615-L629)); cache, if kept, must key on `(organization_id, id)` in a per-instance dict.
3. [calendar_service.py](../calendar_integration/services/calendar_service.py): in `authenticate` ([L405-L425](../calendar_integration/services/calendar_service.py#L405-L425)) and `initialize_without_provider` ([L427-L448](../calendar_integration/services/calendar_service.py#L427-L448)), build and store `self._context`. Replace the moved helpers' bodies with calls into `calendar_service_utils`. Keep the public/`_private` method names on the facade for now.

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_calendar_service_utils.py` — covers timezone conversion + serialization equivalence, and a **regression test proving the lookup returns the right org's calendar** when the same service instance is reused across two organizations (the lru bug).
- Plus the two shared Tests entries above.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Mechanical extraction across multiple symbols plus a correctness fix with a subtle multi-tenant edge; cheaper tiers risk silently preserving the cache bug.

**Reusable skills**: none — internal refactor (no model/view/SQL surface).

Acceptance: `CalendarServiceContext` and `calendar_service_utils` exist; facade delegates the moved helpers to them; the lru regression test passes; full suite green with no test-assertion edits.

### Phase 1 — Extract `RecurrenceManager` helper

**Goal**: Move the generic recurring-exception and bulk-modification engines into a stateless `RecurrenceManager`; the facade's event/blocked/available recurrence methods delegate to it. No behavior change.

**Feature flag**: none — pure refactor, existing recurrence tests are the proof.

Changes:
1. New `@calendar_integration/services/recurrence_manager.py`: `RecurrenceManager` wrapping `_create_recurring_exception_generic` and `_create_recurring_bulk_modification_generic` (currently around [L1887-L2108](../calendar_integration/services/calendar_service.py#L1887-L2108) and [L3733-L4107](../calendar_integration/services/calendar_service.py#L3733-L4107)). It receives the context + the type-specific callbacks (truncate / continuation / record) as parameters, exactly as today.
2. [calendar_service.py](../calendar_integration/services/calendar_service.py): the public recurrence methods (`create_recurring_event_exception`, `create_recurring_event_bulk_modification`, and the blocked/available equivalents) now construct callbacks and call `self._recurrence_manager`.

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_recurrence_manager.py` — exercises the generic engine directly with a synthetic callback set (exception path + bulk-mod path).
- Plus the two shared Tests entries.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` (`[1m]`). The generic engines use callback indirection and span ~600 lines of the most intricate logic in the file; getting the callback contract exactly right is high-stakes.

**Reusable skills**: none.

Acceptance: `RecurrenceManager` owns the two engines; all three entity families' recurrence methods delegate; existing recurrence tests (in `test_calendar_service.py`) green unchanged.

### Phase 2 — Extract `CalendarEventService`

**Goal**: Move single + recurring event CRUD, transfer, and instance/expansion reads into `CalendarEventService`, consuming `RecurrenceManager` + utils. Facade delegates.

**Feature flag**: none — pure refactor.

Changes:
1. New `@calendar_integration/services/calendar_event_service.py`: `create_event`, `create_recurring_event`, `update_event`, `delete_event`, `transfer_event`, `get_recurring_event_instances`, `get_calendar_events_expanded`, `create_recurring_event_exception`, `create_recurring_event_bulk_modification`, `modify_recurring_event_from_date`, `cancel_recurring_event_from_date` (currently spread across [L953-L2407](../calendar_integration/services/calendar_service.py#L953-L2407)). Uses context for adapter/permission/side-effects, utils for serialization/timezone, `RecurrenceManager` for recurrence.
2. [calendar_service.py](../calendar_integration/services/calendar_service.py): facade constructs `self._event_service` post-auth and forwards each of those public methods.

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_calendar_event_service.py` — direct construction with a context fixture; covers create/update/delete + one recurring path. (Bulk of coverage stays in the existing `test_calendar_service.py` exercising the facade.)
- Plus the two shared Tests entries.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` (`[1m]`). Highest fan-in concern; event CRUD shares timezone/adapter/permission/side-effect threads and is consumed by bundle/sync/availability — subtle state coupling.

**Reusable skills**: none.

Acceptance: `CalendarEventService` owns event CRUD; facade delegates; event + recurring-event tests green unchanged.

### Phase 3 — Extract `CalendarBundleService`

**Goal**: Move bundle calendar CRUD and bundle-event fan-out into `CalendarBundleService`, delegating per-child event work to `CalendarEventService`. Facade delegates.

**Feature flag**: none — pure refactor.

Changes:
1. New `@calendar_integration/services/calendar_bundle_service.py`: `create_bundle_calendar`, `update_bundle_calendar`, `_create_bundle_event`, `_update_bundle_event`, `_delete_bundle_event`, `_get_primary_calendar`, `_collect_bundle_attendees` (currently [L803-L951](../calendar_integration/services/calendar_service.py#L803-L951) + bundle-event blocks around [L1432-L1490](../calendar_integration/services/calendar_service.py#L1432-L1490), [L2244-L2269](../calendar_integration/services/calendar_service.py#L2244-L2269)). Receives `CalendarEventService` for child/primary event ops.
2. [calendar_service.py](../calendar_integration/services/calendar_service.py): facade wires `self._bundle_service` (passing the event service) and forwards `create_bundle_calendar` / `update_bundle_calendar`; the event service routes bundle-calendar events to it.

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_calendar_bundle_service.py` — bundle create/update invariants (primary calendar, child reconciliation) with a stubbed event service.
- Plus the two shared Tests entries.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Self-contained invariants with one clean dependency (event service).

**Reusable skills**: none.

Acceptance: `CalendarBundleService` owns bundle logic; facade + event service delegate to it; bundle tests green unchanged.

### Phase 4 — Extract `AvailabilityService`

**Goal**: Move available-times, blocked-times, their recurrence handling, and the availability/unavailability window queries into `AvailabilityService`. Facade delegates.

**Feature flag**: none — pure refactor.

Changes:
1. New `@calendar_integration/services/availability_service.py`: `create_available_time`, `bulk_create_availability_windows`, `batch_modify_available_times`, `create_blocked_time`, `get_available_times_expanded`, `get_blocked_times_expanded`, `get_availability_windows_in_range`, `get_unavailable_time_windows_in_range`, and the available/blocked recurrence-exception + bulk-mod + modify/cancel methods (currently [L3261-L4211](../calendar_integration/services/calendar_service.py#L3261-L4211)). Plus the private interval helpers `_remove_available_time_windows_that_overlap_with_blocked_times_and_events`, `_subtract_busy_intervals`. Uses `RecurrenceManager`; reads events via `CalendarEventService` (busy intervals).
2. [calendar_service.py](../calendar_integration/services/calendar_service.py): facade wires `self._availability_service` and forwards those public methods (including the ones `views.py` and `public_api/queries.py` call: `get_availability_windows_in_range`).

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_availability_service.py` — window subtraction (all-time minus blocked minus events) + one recurring blocked-time path.
- Plus the two shared Tests entries.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` (`[1m]`). Interval-subtraction math feeding public availability queries; an off-by-one in window merging is a silent correctness regression.

**Reusable skills**: none.

Acceptance: `AvailabilityService` owns availability + blocked/available times; facade delegates; availability/public-api availability tests green unchanged.

### Phase 5 — Extract `CalendarSyncService`

**Goal**: Move calendar/account/org-resource import and the sync state machine into `CalendarSyncService`, delegating event materialization to `CalendarEventService`. Facade delegates.

**Feature flag**: none — pure refactor.

Changes:
1. New `@calendar_integration/services/calendar_sync_service.py`: `request_calendars_import`, `import_account_calendars`, `request_calendar_sync`, `sync_events`, `request_organization_calendar_resources_import`, `import_organization_calendar_resources`, and the sync internals `_execute_calendar_sync`, `_process_events_for_sync`, `_apply_sync_changes`, `_link_orphaned_recurring_instances`, `_execute_organization_calendar_resources_import` (currently [L450-L763](../calendar_integration/services/calendar_service.py#L450-L763) + [L2422-L2985](../calendar_integration/services/calendar_service.py#L2422-L2985)). Uses context adapter + `CalendarEventService`.
2. [calendar_service.py](../calendar_integration/services/calendar_service.py): facade wires `self._sync_service` and forwards the public sync/import methods the Celery tasks ([calendar_sync_tasks.py](../calendar_integration/tasks/calendar_sync_tasks.py)) and `organizations/services.py` call.

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_calendar_sync_service.py` — one full sync diff/merge cycle against a fake adapter. (Existing [test_calendar_sync_tasks.py](../calendar_integration/tests/tasks/test_calendar_sync_tasks.py) remains the integration gate via the facade.)
- Plus the two shared Tests entries.

**Suggested AI model**: Tier 4 — `claude-opus-4-7` (`[1m]`). The sync state machine is the most tightly-coupled cluster (diff/merge, orphan linking, webhook-event tracking); needs whole-flow comprehension.

**Reusable skills**: none.

Acceptance: `CalendarSyncService` owns import + sync; facade + Celery tasks delegate; sync-task tests green unchanged.

### Phase 6 — Extract `CalendarWebhookService`

**Goal**: Move webhook subscription lifecycle and webhook-triggered sync into `CalendarWebhookService`, delegating sync triggering to `CalendarSyncService`. Facade delegates.

**Feature flag**: none — pure refactor.

Changes:
1. New `@calendar_integration/services/calendar_webhook_service.py`: `create_calendar_webhook_subscription`, `request_webhook_triggered_sync`, `process_webhook_notification`, `handle_webhook`, `delete_webhook_subscription`, `refresh_webhook_subscription`, `list_webhook_subscriptions`, `get_webhook_health_status` (currently [L4244-L4671](../calendar_integration/services/calendar_service.py#L4244-L4671)). Uses context adapter + `CalendarSyncService` (and the lru-fixed `get_calendar_by_external_id` from utils, [L4463](../calendar_integration/services/calendar_service.py#L4463)).
2. [calendar_service.py](../calendar_integration/services/calendar_service.py): facade wires `self._webhook_service` and forwards the methods [mutations.py](../calendar_integration/mutations.py) and the webhook management commands call.

Spec use-case: shared scaffolding — no use-case.

Tests:
- **Unit**: `@calendar_integration/tests/services/test_calendar_webhook_service.py` — subscription create/refresh/delete + notification parse with a fake adapter.
- Plus the two shared Tests entries.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Leaf concern with one dependency (sync); webhook tests ([test_google_calendar_webhooks.py](../calendar_integration/tests/test_google_calendar_webhooks.py), [test_microsoft_calendar_webhooks.py](../calendar_integration/tests/test_microsoft_calendar_webhooks.py)) are a strong net.

**Reusable skills**: none.

Acceptance: `CalendarWebhookService` owns webhooks; facade + mutations delegate; webhook tests green unchanged.

### Phase 7 — Shrink the facade and finalize wiring

**Goal**: With all concerns extracted, reduce `CalendarService` to auth-state ownership + sub-service construction + one-line delegations; remove now-dead private helpers; confirm DI wiring and `__init__.py` exports.

**Feature flag**: none — pure cleanup.

Changes:
1. [calendar_service.py](../calendar_integration/services/calendar_service.py): delete any private helper fully migrated into a sub-service/util; ensure `authenticate` / `initialize_without_provider` build the context and (lazily) the sub-services; every public method is a single delegation line. Target: facade well under ~600 lines.
2. [di_core/containers.py](../di_core/containers.py#L95-L105): confirm the `calendar_service` provider signature is unchanged (still injects `calendar_side_effects_service` + `calendar_permission_service`); `calendar_group_service` keeps injecting `calendar_service`. No sub-service providers added.
3. `grep` for stale references to moved symbols across `calendar_integration/` (and any test reaching a moved `_private` name) and fix imports only.
4. Update module docstrings / `@calendar_integration/services/__init__.py` exports.

Spec use-case: shared scaffolding — no use-case.

Tests:
- Full `calendar_integration` suite green unchanged.
- `grep -rn "_create_recurring_exception_generic\|_subtract_busy_intervals\|_process_events_for_sync" calendar_integration/services/calendar_service.py` returns nothing (logic fully migrated).

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Mechanical deletion + import fixups; behavior already locked by prior phases.

**Reusable skills**: none.

Acceptance: facade is delegation-only and under target size; DI signature unchanged; full suite green; no stale references to migrated internals.

## 6. Risk & Rollout Notes

- **No feature flag** (pure refactor — see **Guiding Decisions**). Rollout is ordinary merge per phase; each phase is independently mergeable and independently revertible because the facade's public surface never changes.
- **No migrations, no locks, no partitions, no backfill** — zero DB impact.
- **Performance guardrail**: the single real perf risk is re-authenticating per sub-service. Mitigated by the shared `CalendarServiceContext` built once in `authenticate()`; reviewers must verify no sub-service calls `get_calendar_adapter_for_account` or re-constructs an adapter. The lru_cache fix could add DB queries if naively removed — keep an org-scoped per-instance cache so query counts don't regress; assert with a query-count test if the suite has the harness.
- **Coupling risk**: events are consumed by bundle, availability, and sync. Extracting events (Phase 2) before its consumers is deliberate; if a consumer phase surfaces a hidden dependency, the fix is local to the facade's sibling wiring, not the call sites.
- **Rollback**: revert the offending phase's commit — the prior phase's facade delegation is fully functional on its own. No data or schema to unwind.
- **Test-as-contract risk**: if the existing suite needs assertion edits (beyond import-path fixes for moved `_private` symbols), treat it as a behavior change and stop — investigate before editing the test.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should `CalendarService` later be deprecated in favor of direct sub-service injection at call sites? | Defer — out of scope here. Revisit once the facade has been stable for a release; it's a mechanical follow-up plan. | Eng lead |
| Should sub-services become first-class DI-container providers (testable in isolation via the container)? | No for now — facade-constructed keeps DI wiring untouched and the perf-critical single-auth contract local. Reconsider if a sub-service gains external consumers. | Eng lead |
| Keep an org-scoped cache on calendar lookups, or drop caching entirely? | Keep a per-instance `{(org_id, id): Calendar}` dict to avoid query-count regression; drop only if a query-count test shows it's unused on hot paths. | Implementer (Phase 0) |

## 8. Touch List

**Phase 0** — foundation
- create `@calendar_integration/services/calendar_service_context.py`
- create `@calendar_integration/services/calendar_service_utils.py`
- create `@calendar_integration/tests/services/test_calendar_service_utils.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py) (authenticate/init build context; delegate moved helpers; fix lookups)

**Phase 1** — recurrence
- create `@calendar_integration/services/recurrence_manager.py`
- create `@calendar_integration/tests/services/test_recurrence_manager.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py)

**Phase 2** — events
- create `@calendar_integration/services/calendar_event_service.py`
- create `@calendar_integration/tests/services/test_calendar_event_service.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py)

**Phase 3** — bundle
- create `@calendar_integration/services/calendar_bundle_service.py`
- create `@calendar_integration/tests/services/test_calendar_bundle_service.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py)

**Phase 4** — availability
- create `@calendar_integration/services/availability_service.py`
- create `@calendar_integration/tests/services/test_availability_service.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py)

**Phase 5** — sync
- create `@calendar_integration/services/calendar_sync_service.py`
- create `@calendar_integration/tests/services/test_calendar_sync_service.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py)

**Phase 6** — webhooks
- create `@calendar_integration/services/calendar_webhook_service.py`
- create `@calendar_integration/tests/services/test_calendar_webhook_service.py`
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py)

**Phase 7** — facade shrink + wiring
- edit [calendar_service.py](../calendar_integration/services/calendar_service.py) (delegation-only)
- edit [di_core/containers.py](../di_core/containers.py) (confirm signature unchanged)
- edit `@calendar_integration/services/__init__.py` (exports)
