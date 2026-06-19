# calendarEvents `userId` Filter — Implementation Plan

## 1. Goals

1. Add an optional `userId` argument to the `calendarEvents` query in [public_api/queries.py](../public_api/queries.py) so the Scheduler/Calendar screen can request a single provider's events in one call, instead of resolving that user's calendars client-side and issuing one query per calendar.
2. Resolve "a user's events" server-side as the recurring-expanded events on every calendar owned by that user (via `CalendarOwnership`), within the existing `startDatetime`/`endDatetime` window, deduped so each occurrence appears once.
3. Keep the new path fully org-scoped (only calendars in the caller's organization) and honor the existing scoped-token owner constraint (a per-owner token never sees another owner's events through `userId`).
4. Compose with the existing `calendarId` argument: when both are supplied, the calendar must be owned by `userId` or the result is empty (intersection semantics).

**Non-goals:**
- No change to the `eventId` lookup branch of `calendarEvents`.
- No new feature flag — `userId` is an optional argument that defaults to `None`; omitting it leaves every existing caller byte-for-byte unchanged.
- No frontend/Scheduler-screen changes (this plan delivers the API only; the screen consumes it separately).
- No new GraphQL resource or change to `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` (`calendarEvents` already maps to `CALENDAR_EVENT`).
- No write/mutation surface; read-only.
- No cross-repo / producer work.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Expansion strategy** | Add a new multi-calendar service method (`get_calendar_events_expanded_for_calendars`) rather than looping the existing per-calendar method in the resolver. A single method keeps recurrence/bundle/dedup logic in the service layer (where the existing single-calendar expansion lives) and lets the resolver stay thin. Chosen over a resolver-side loop because the dedup-across-calendars rule belongs with the expansion logic, not the GraphQL layer. |
| **Compose rule (`calendarId` + `userId`)** | Intersect: the requested calendar must be among the user's owned calendars, else the result is empty. `userId` acts as an ownership guard on `calendarId`. Avoids the footgun of silently ignoring one argument. |
| **Required arguments** | `userId` + `startDatetime` + `endDatetime` is a new valid combo; `startDatetime`/`endDatetime` stay mandatory whenever `eventId` is absent. Bounds recurrence-expansion cost across potentially many calendars. |
| **Dedup** | Collapse by `(event id, occurrence start_time)` so an event reachable through multiple owned calendars (e.g. a BUNDLE plus one of its children) appears once. Reuses the bundle-dedup spirit already in the single-calendar method. |
| **Org scoping** | The owned-calendar resolution is constrained to `Calendar.objects.filter_by_organization(org.id)`; the event query filters `organization_id` too. A `userId` for a user with no owned calendars in the org → empty list (not an error, no existence oracle). |
| **Scoped-token interaction** | When the request carries a scoped (per-owner) token, the resolved owned-calendar set is intersected with `scoped_calendar_ids(...)`. A scoped token asking for a different user's `userId` gets the empty intersection → empty list, matching the no-existence-leak behavior of the other fields. |
| **No feature flag** | Purely additive optional argument; the new code path is unreachable unless `userId` is passed. Existing callers omit it and execute the identical pre-existing branch. (Per the plan-feature flag rule, this is the legitimate "purely additive surface" skip.) |

## 3. Data Model Changes

None. No new tables, columns, or migrations. `CalendarOwnership` already exists with FK `calendar` (`related_name="ownerships"`) and FK `user` (`related_name="calendar_ownerships"`); see [calendar_integration/models.py:221-247](../calendar_integration/models.py#L221-L247). Traversal `Calendar.objects.filter(ownerships__user_id=<id>)` is the same one the `calendars` query already uses.

### 3.1 Type plumbing

No new types. The new service method returns `list[CalendarEvent]` (same as the existing `get_calendar_events_expanded`), and the resolver continues to return `list[CalendarEventGraphQLType]`.

## 4. API Design

### 4.1 `calendarEvents` query — new optional argument

Field: `Query.calendar_events` in [public_api/queries.py](../public_api/queries.py).

New signature (added argument in **bold** intent):

```
calendarEvents(
  calendarId: Int = null,
  userId: Int = null,          # NEW
  startDatetime: DateTime = null,
  endDatetime: DateTime = null,
  eventId: Int = null,
): [CalendarEventGraphQLType]
```

Resolution rules:
- `eventId` present → unchanged existing branch (ignores `userId`).
- Neither `calendarId` nor `userId` → same `GraphQLError` for missing required parameters (now also names `userId` as an alternative to `calendarId`).
- `userId` present (with `startDatetime` + `endDatetime`) → resolve owned + org-scoped (+ token-scoped) calendar IDs; if `calendarId` also present, intersect down to `{calendarId} ∩ owned`; call the new multi-calendar service method; return deduped expanded events.
- `calendarId` present, `userId` absent → unchanged existing single-calendar branch.
- Missing `startDatetime`/`endDatetime` when `eventId` absent → same `GraphQLError` as today.

Errors: reuses the existing `GraphQLError("Missing required parameters…")` text, extended to mention `userId`. No new error types. Permission classes unchanged: `[IsAuthenticated, OrganizationResourceAccess]`.

## 5. Phased Rollout

### Phase 1 — Multi-calendar expansion service method

**Goal**: Add a service method that expands recurring events across a set of calendars (org-scoped) and dedups occurrences, so the resolver can fetch one user's events in a single call. Ship value: none user-visible on its own (no field wired yet); this is the reusable core the resolver phase consumes.

**Feature flag**: none — internal service method, no reachable behavior until Phase 2 wires it.

Changes:
1. [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py): add `get_calendar_events_expanded_for_calendars(self, calendar_ids: collections.abc.Iterable[int], start_date, end_date, optimize_queryset=None) -> list[CalendarEvent]`. Generalize the existing `get_calendar_events_expanded` base queryset (the `organization_id` + `calendar`/`calendar__in` filter) to `.filter(organization_id=self._context.organization.id, calendar_fk__in=calendar_ids)`. Keep the non-recurring + recurring-master expansion and the final sort. Replace the BUNDLE-only dedup with a general dedup keyed by `(event.id, event.start_time)` that also preserves the existing bundle-representation skip rule (drop `is_bundle_representation`, keep `is_bundle_primary` once). Empty/`None` `calendar_ids` → return `[]` without hitting the DB.
2. [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py): add the thin facade `get_calendar_events_expanded_for_calendars(...)` delegating to the event service, mirroring the existing `get_calendar_events_expanded` facade.
3. Consider refactoring the single-calendar `get_calendar_events_expanded` to delegate to the new method (resolve the one calendar — or bundle children — to ids, call through). Only do this if it leaves the single-calendar behavior byte-for-byte identical under existing tests; otherwise leave it untouched and accept minor duplication.

Spec use-case: shared scaffolding — no use-case yet (consumed by Phase 2).

Tests:
- **Unit**: [calendar_integration/tests/test_calendar_event_service.py](../calendar_integration/tests/test_calendar_event_service.py) (or the existing expansion test module) — covers: (a) events from two distinct calendars returned together; (b) a recurring master on one calendar expands to in-range occurrences; (c) an event reachable via a BUNDLE and its child appears once (dedup); (d) empty `calendar_ids` → `[]` with no query; (e) events outside the range excluded; (f) calendars in another organization excluded even if their ids are passed (org guard).
- **Integration**: none (no field wired yet).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Touches recurrence-expansion internals + new dedup; needs care to preserve single-calendar parity.

**Reusable skills**: `write-tests` (service-layer unit tests following the fixture catalog).

Acceptance: `get_calendar_events_expanded_for_calendars([cal_a, cal_b], start, end)` returns the union of both calendars' in-range expanded events, deduped, org-scoped, with single-calendar `get_calendar_events_expanded` behavior unchanged under its existing tests.

### Phase 2 — Wire `userId` into the `calendarEvents` query

**Goal**: The Scheduler/Calendar screen can call `calendarEvents(userId, startDatetime, endDatetime)` and receive exactly that provider's recurring-expanded events, org- and token-scoped, optionally intersected with `calendarId`.

**Feature flag**: none — new branch only runs when `userId` is supplied; omitting it preserves the current resolver path exactly.

Changes:
1. [public_api/queries.py](../public_api/queries.py): add `user_id: int | None = None` to `calendar_events`. After the `eventId` branch, add the `userId` branch:
   - Resolve `org = _get_org(info)`.
   - Build owned set: `owned_ids = set(Calendar.objects.filter_by_organization(org.id).filter(ownerships__user_id=user_id).values_list("id", flat=True))`.
   - Apply token scope: if `system_user is not None` and `scoped_calendar_ids(system_user, org)` returns a set, intersect `owned_ids` with it.
   - If `calendarId` also supplied, intersect `owned_ids` with `{calendar_id}`.
   - Require `start_datetime`/`end_datetime` (reuse existing missing-params guard).
   - Initialize the calendar service for the org (reuse the `initialize_without_provider` pattern from `_prepare_service_and_calendar`, without resolving a single Calendar) and call `get_calendar_events_expanded_for_calendars(owned_ids, start_datetime, end_datetime)`.
   - Return `[]` early when `owned_ids` is empty.
2. Extend the missing-required-parameters `GraphQLError` message to mention `userId` as an alternative to `calendarId`.
3. Leave the existing `calendarId`-only and `eventId` branches untouched.

Spec use-case: "List a single provider's events on the Scheduler/Calendar screen" (the feature's sole use-case).

Tests:
- **Integration**: [public_api/tests/test_queries.py](../public_api/tests/test_queries.py) — covers:
  - `userId` filter returns only that user's events (events on a calendar owned by another user are excluded).
  - Organization boundary: a `userId` whose owned calendars belong to another org → empty; events in another org never leak.
  - `calendarId` + `userId` intersection: calendar owned by the user → events returned; calendar not owned by the user → empty.
  - Recurring expansion: a recurring master on a user-owned calendar expands to in-range instances through the `userId` path.
  - Scoped-token: a per-owner token requesting its own `userId` sees its events; requesting a different `userId` → empty (no existence leak); org-wide token sees the full owned set.
  - **Backwards-compatibility (additive-arg proof)**: existing `calendarId`-only and `eventId` queries return identical results with `userId` omitted (covered by leaving existing tests green + one explicit "omit userId == today" assertion).
- **Unit**: none beyond Phase 1.
- **E2E**: none — backend-only, no browser flow reached.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Resolver branching with security-sensitive org + token scoping and several integration tests.

**Reusable skills**: `graphql-public-query` (field-wiring + permission conventions for the public GraphQL surface); `write-tests` (integration tests under [public_api/tests/test_queries.py](../public_api/tests/test_queries.py)).

Acceptance: `calendarEvents(userId, startDatetime, endDatetime)` returns exactly the recurring-expanded events on that user's owned, org-scoped (and token-scoped) calendars; `calendarId`+`userId` intersects; omitting `userId` leaves existing behavior unchanged; full `public_api` test suite green.

## 6. Risk & Rollout Notes

- **No feature flag** — additive optional argument; rollout is the merge itself. The new branch is unreachable for existing callers (they omit `userId`).
- **No migrations / no locks / no partition changes / no backfill** — read-only, no schema change.
- **Recurrence-expansion cost**: a `userId` spanning many owned calendars expands recurrence across all of them. Mitigated by keeping `startDatetime`/`endDatetime` mandatory (bounded window, same guard as the existing path). If a provider owns an unusually large number of calendars, the per-calendar expansion is linear in calendars × occurrences; acceptable for the Scheduler screen's single-provider, bounded-window use. Watch query volume after the screen adopts it.
- **Security**: the org guard (`filter_by_organization`) and scoped-token intersection (`scoped_calendar_ids`) must both apply before the service call — a regression here is a cross-tenant / cross-owner leak. Integration tests assert both boundaries explicitly. Phase 1's service method also filters `organization_id` defensively so a bad id set cannot cross orgs.
- **Rollback**: revert the two commits (Phase 2 then Phase 1). No data or schema to unwind; reverting restores the prior resolver and removes the unused service method.

## 7. Open Questions

- **Per-provider calendar cardinality**: rough upper bound of calendars one provider owns is unconfirmed. Recommended default: proceed with the bounded-window guard; if profiling later shows hot expansion, add a server-side cap on owned-calendar count or a narrower max window (mirroring `MAX_CODE_GATED_RANGE`). Owner: eng, post-adoption.
- **Sort/pagination on the `userId` path**: the service returns events sorted by `start_time`, unpaginated (matching the existing `calendarId` path). If the Scheduler screen needs pagination across a wide window, that is a follow-up. Recommended default: ship unpaginated to match the existing field contract.

## 8. Touch List

**Phase 1 — Multi-calendar expansion service method**
- Edit: [calendar_integration/services/calendar_event_service.py](../calendar_integration/services/calendar_event_service.py) — new `get_calendar_events_expanded_for_calendars`; optional refactor of `get_calendar_events_expanded` to delegate.
- Edit: [calendar_integration/services/calendar_service.py](../calendar_integration/services/calendar_service.py) — facade delegating to the event service.
- Edit/Add: `@calendar_integration/tests/test_calendar_event_service.py` — unit tests for the new method.

**Phase 2 — Wire `userId` into the query**
- Edit: [public_api/queries.py](../public_api/queries.py) — `user_id` argument + resolution branch + extended error message.
- Edit/Add: `@public_api/tests/test_queries.py` — integration tests for the `userId` path, intersection, org/token boundaries, recurrence expansion, and backwards compatibility.
