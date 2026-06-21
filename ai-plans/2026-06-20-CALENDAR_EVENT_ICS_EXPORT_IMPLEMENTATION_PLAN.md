# Calendar Event ICS Export — Implementation Plan

> No sibling `..._SPEC.md` exists for this feature. Decisions below were captured via a Step-0 interrogation with the requester and are recorded inline in **Guiding Decisions**. If the scope grows beyond a single-event export, write a SPEC first.

## 1. Goals

1. Generate a valid RFC 5545 iCalendar (`.ics`) document for a single `CalendarEvent`, including UID, summary, start/end (timezone-aware), description, location, status, sequence, organizer, and attendees.
2. Expose the document over an **internal REST** endpoint as a file download: `GET /calendar-events/{id}/ics/` returning `Content-Type: text/calendar` with a `Content-Disposition: attachment` header.
3. Expose the document over the **public GraphQL** API as a raw string field: `eventIcs(eventId: Int!): String`, bearer-token authenticated and owner-scoped like the existing `calendarEvents` query.
4. Represent a recurring event as a **single VEVENT** carrying its `RRULE` line, plus `EXDATE` lines for cancelled occurrences — clients expand the series.

**Non-goals:**
- No multi-event export. Bundle-primary events, calendar groups, and `bulk_modification_parent` series are **not** expanded into multiple VEVENTs in v1 — exactly one `CalendarEvent` → one VEVENT (+ RRULE).
- No full ICS *feed* (subscribable `webcal://` calendar of many events). Single-event export only.
- No `VTIMEZONE` block authoring by hand — rely on the `icalendar` library + IANA tz ids; emit UTC `DTSTART`/`DTEND` with `TZID` parameters as the library produces them.
- No `VALARM`/reminder block (explicitly dropped in interrogation).
- No write/import side (parsing uploaded ICS) — export only.
- No feature flag and no new flag mechanism (the repo has none today; this surface is purely additive).
- No internal GraphQL field — public API GraphQL only.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **ICS generation** | Add the maintained [`icalendar`](https://pypi.org/project/icalendar/) PyPI package as a project dependency. Hand-rolling RFC 5545 escaping, line-folding, and `RRULE`/`ATTENDEE` formatting is a known bug source; the library handles all of it. |
| **Single source of truth** | One pure builder service, `CalendarEventICSService.build_ics(event) -> bytes`, lives in `calendar_integration/services/`. Both the REST action and the GraphQL field call it — no duplicated serialization logic across surfaces. |
| **Builder is pure / read-only** | The builder takes a fully-loaded `CalendarEvent` (with related attendees/recurrence prefetched) and returns bytes. It performs no DB writes, no auth, no org resolution — those stay in the REST/GraphQL layers that already enforce them. Keeps it trivially unit-testable. |
| **Recurrence shape** | Single VEVENT + `RRULE` (from `RecurrenceRule.to_rrule_string()`), with `EXDATE` lines derived from `EventRecurrenceException` cancelled occurrences. Clients expand. Avoids unbounded occurrence expansion and a date-range parameter. |
| **UID stability** | `UID` = `external_id` when present, else a deterministic synthetic id `event-{id}@{organization-domain-or-fixed-namespace}`. Stable UID lets re-downloads update (not duplicate) the event in a client calendar. |
| **STATUS / SEQUENCE** | `STATUS:CONFIRMED` for normal events, `STATUS:CANCELLED` when the event is itself a cancelled exception. `SEQUENCE` from a monotonic source (updated-at-derived integer or `0` if none) so clients accept updates. |
| **Attendee exposure** | `ATTENDEE` lines include internal attendees (via `EventAttendance` → membership user email) and `ExternalAttendee` emails; `ORGANIZER` from the event's organizing context. Accepted as intended — whoever can read the event can already see participants through existing endpoints. |
| **Auth reuse** | REST reuses `CalendarEventPermission` + `get_object()` org scoping already on `CalendarEventViewSet`. GraphQL reuses `IsAuthenticated` + `OrganizationResourceAccess` + `scoped_calendar_ids` owner-scoping exactly as `calendarEvents` does. No new auth code. |
| **Resource mapping** | The new GraphQL field `eventIcs` maps to `PublicAPIResources.CALENDAR_EVENT` in `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING` — same resource a caller already needs to read events. |
| **No feature flag** | Brand-new REST action at a new sub-path + brand-new public query field. No existing code reads or writes through these; no existing response shape changes. Adding a flag would require introducing a flag library that does not exist in the repo — out of proportion to an additive read-only surface. |
| **Failure mode** | Unknown / out-of-org / out-of-scope event id → REST `404` (via existing `get_object`), GraphQL → `null` (no existence leak, matching `calendarEvents` `eventId` branch). Malformed event data (e.g. missing tz) → `500` is acceptable in v1; builder asserts required fields and lets the error surface. |

## 3. Data Model Changes

**None.** This feature is read-only over existing models. Relevant existing models the builder reads (all in @calendar_integration/models.py):

- `CalendarEvent` (lines 1070-1204) — `title`, `description`, `external_id`, `start_time` / `end_time` (timezone-aware `GeneratedField`s), `timezone`, `recurrence_rule`.
- `RecurringMixin` (lines 724-787) — `recurrence_rule`, `recurrence_id`, exception flags.
- `RecurrenceRule` (lines 511-717) — `to_rrule_string()` (line 570) produces the `RRULE` value directly.
- `EventAttendance` (lines 438-481) + `EventExternalAttendance` (lines 409-436) — attendee sources for `ATTENDEE` lines.
- `EventRecurrenceException` (lines 1264-1300) — cancelled / modified occurrences → `EXDATE`.

### 3.1 Type plumbing

No new TypedDicts/dataclasses required. The builder's public signature is `build_ics(event: CalendarEvent) -> bytes`. If a richer return is wanted later (filename + content-type), introduce a small dataclass then — not in v1 (GraphQL returns a raw string; REST sets headers itself).

## 4. API Design

### 4.1 REST — `GET /calendar-events/{id}/ics/`

- **Method / path**: `GET /calendar-events/{id}/ics/` — a `detail=True` `@action(methods=["get"], url_path="ics", url_name="ics")` on the existing `CalendarEventViewSet` (@calendar_integration/views.py:848).
- **Auth / scope**: existing `CalendarEventPermission` + `get_object()` (org-scoped queryset via `get_queryset`). No change.
- **Success**: `200`, body = ICS bytes, `Content-Type: text/calendar; charset=utf-8`, `Content-Disposition: attachment; filename="event-{id}.ics"`. Return via Django `HttpResponse` (small, bounded single-event payload — no streaming needed; the `StreamingHttpResponse` pattern in @audit/admin.py is for unbounded CSV).
- **Errors**: `404` for unknown / out-of-org id (existing behavior); `403` for permission failures (existing).
- **OpenAPI**: `@extend_schema(summary=..., responses={(200, "text/calendar"): OpenApiTypes.BINARY})`; regenerate `schema.yml`.

### 4.2 GraphQL — `eventIcs(eventId: Int!): String`

- **Location**: new field on the `Query` class in @public_api/queries.py, decorated `@strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])` (or plain `@strawberry.field` — match the surrounding `calendarEvents` decorator so the permission class resolves the field name).
- **Resolver**: resolve org via `_get_org(info)`; fetch `CalendarEvent.objects.filter_by_organization(org.id).filter(id=event_id)`; apply `scoped_calendar_ids` owner-scoping exactly as the `eventId` branch of `calendar_events` (@public_api/queries.py:349-358); return `None` if not found / out of scope; else `build_ics(event).decode("utf-8")`.
- **Auth mapping**: add `"eventIcs": PublicAPIResources.CALENDAR_EVENT` to `FIELD_TO_RESOURCE_MAPPING` (@public_api/permissions.py).
- **Return**: raw ICS text as `String`.

## 5. Phased Rollout

### Phase 1 — Add `icalendar` dependency + core ICS builder

**Goal**: a pure `CalendarEventICSService.build_ics(event) -> bytes` that emits a valid single-VEVENT calendar with UID, SUMMARY, DTSTART/DTEND (timezone-aware), DESCRIPTION, LOCATION, STATUS, SEQUENCE, DTSTAMP. Ship value: none user-visible on its own — this is the shared engine both surfaces consume; justified because REST and GraphQL must not duplicate ICS serialization.

**Feature flag**: none — purely additive scaffolding (no reachable behavior, no existing caller).

Changes:
1. `pyproject.toml`: add `icalendar` to dependencies; `uv lock`. (No env var, no settings change.)
2. New file @calendar_integration/services/ics_service.py: `CalendarEventICSService` with `build_ics(self, event: CalendarEvent) -> bytes` building one `icalendar.Calendar` + one `icalendar.Event` (`PRODID`/`VERSION`, `UID` per **Guiding Decisions** UID rule, `SUMMARY`, `DTSTART`/`DTEND` from `event.start_time`/`event.end_time`, `DTSTAMP`, `DESCRIPTION`, `LOCATION`, `STATUS`, `SEQUENCE`). Return `cal.to_ical()`.
3. Export the service from @calendar_integration/services/__init__.py following the existing service-export pattern.
4. Do **not** wire DI yet — the builder is a stateless pure object; instantiate directly. (Revisit only if it needs injected collaborators.)

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: @calendar_integration/tests/test_ics_service.py — builds ICS for a plain (non-recurring) event; asserts `BEGIN:VCALENDAR`/`BEGIN:VEVENT`, correct `UID` (external-id and synthetic-fallback cases), `SUMMARY`, `DTSTART`/`DTEND` reflect the event tz, `DESCRIPTION`/`LOCATION` present and properly escaped (commas, semicolons, newlines), `STATUS:CONFIRMED`, `SEQUENCE`. Parse the output back with `icalendar.Calendar.from_ical(...)` to prove validity rather than string-matching only.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` (step up to `claude-sonnet-4-6` if escaping/tz edge tests need iteration) / `gpt-5-mini` / `gemini-2.5-flash`. Single-file library wrapper against an established model; the only subtlety is timezone + escaping correctness, which the library handles.

**Reusable skills**: `write-tests` (for the unit test under `calendar_integration/tests/`).

Acceptance: `CalendarEventICSService().build_ics(event)` returns bytes that round-trip through `icalendar.Calendar.from_ical` with the expected `UID`/`SUMMARY`/`DTSTART`/`DTEND`/`STATUS`, for both an external-id event and a synthetic-uid event.

---

### Phase 2 — Add participants + recurrence to the builder

**Goal**: extend `build_ics` so the VEVENT carries `ORGANIZER`, `ATTENDEE` lines (internal `EventAttendance` member emails + `ExternalAttendee` emails), `RRULE` (from `RecurrenceRule.to_rrule_string()`), and `EXDATE` lines from cancelled `EventRecurrenceException` occurrences; emit `STATUS:CANCELLED` when the event is itself a cancelled exception. Ship value: none on its own — completes the engine before it is exposed.

**Feature flag**: none — purely additive scaffolding.

Changes:
1. @calendar_integration/services/ics_service.py: add `ORGANIZER` and `ATTENDEE` emission (reading the prefetched attendance relations); add `RRULE` when `event.recurrence_rule` is set; add `EXDATE` from cancelled occurrences; set `STATUS:CANCELLED` for cancelled-exception events.
2. Define the exact attendee-email resolution (internal membership user email vs external attendee email) and `PARTSTAT`/`ROLE` mapping in one private helper so it is testable in isolation.
3. Ensure callers prefetch attendee + recurrence relations to avoid N+1 (document the required `select_related`/`prefetch_related` set in the service docstring; the REST/GraphQL phases apply it).

Spec use-case: shared scaffolding — no use-case yet.

Tests:
- **Unit**: extend @calendar_integration/tests/test_ics_service.py — a recurring event emits exactly one VEVENT with the expected `RRULE`; cancelled occurrences appear as `EXDATE`; internal + external attendees produce correctly-formatted `ATTENDEE` lines with emails; `ORGANIZER` present; a cancelled-exception event emits `STATUS:CANCELLED`. Round-trip parse to validate.

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Recurrence + exception + attendee mapping is the substantive logic; cross-field correctness and tz-aware `EXDATE` warrant the stronger tier.

**Reusable skills**: `write-tests`.

Acceptance: for a recurring event with one cancelled occurrence and one internal + one external attendee, `build_ics` output parses to a single VEVENT whose `RRULE` matches `recurrence_rule.to_rrule_string()`, whose `EXDATE` contains the cancelled occurrence, and which carries one `ORGANIZER` and two `ATTENDEE` lines.

---

### Phase 3 — REST ICS download action

**Goal**: a tenant-scoped, permission-checked `GET /calendar-events/{id}/ics/` returns the event's `.ics` as a file download. This is the first user-visible deliverable.

**Feature flag**: none — new sub-path on an existing viewset; no existing route/response changes.

Changes:
1. @calendar_integration/views.py (`CalendarEventViewSet`, line 848): add `@action(detail=True, methods=["get"], url_path="ics", url_name="ics")` named e.g. `download_ics`. Resolve the event with `self.get_object()` (keeps org scoping + `CalendarEventPermission`), applying the attendee/recurrence prefetch set documented in Phase 2.
2. Call `CalendarEventICSService().build_ics(event)`; return `HttpResponse(ics_bytes, content_type="text/calendar; charset=utf-8")` with `Content-Disposition: attachment; filename="event-{id}.ics"`.
3. Decorate with `@extend_schema(summary="Download calendar event ICS", responses={200: OpenApiTypes.BINARY})`; regenerate `schema.yml`.
4. No `routes.py` change — `@action` registers under the existing `calendar-events` router entry (@calendar_integration/routes.py).

Spec use-case: **REST ICS export** — download a single event as `.ics` via the internal REST API.

Tests:
- **Integration**: @calendar_integration/tests/test_views.py (or the existing event-viewset test module) — authenticated in-org user gets `200`, `Content-Type: text/calendar`, `Content-Disposition` attachment, and a body that parses as valid iCalendar with the right `UID`; user without `CalendarEventPermission` gets `403`; event in another org gets `404`; unknown id gets `404`.

**Suggested AI model**: Tier 2 — `claude-haiku-4-5` / `gpt-5-mini` / `gemini-2.5-flash`. Thin `@action` over an established viewset + service; exact precedent for custom actions exists at @calendar_integration/views.py:233-259.

**Reusable skills**: `create-rest-endpoint` (action wiring + `schema.yml` regen); `write-tests`.

Acceptance: `GET /calendar-events/{id}/ics/` as an in-org member returns `200` with `Content-Type: text/calendar`, an attachment filename, and a body parseable by `icalendar`; cross-org id returns `404`; `schema.yml` documents the action.

---

### Phase 4 — Public GraphQL `eventIcs` query

**Goal**: external integrations fetch a single event's `.ics` as a string via `eventIcs(eventId: Int!)`, bearer-authenticated and owner-scoped identically to `calendarEvents`. Second user-visible deliverable; independent of Phase 3.

**Feature flag**: none — new field on the public schema; no existing field changes.

Changes:
1. @public_api/queries.py: add `def event_ics(self, info, event_id: int) -> str | None` decorated to match `calendar_events` (`@strawberry_django.field(permission_classes=[IsAuthenticated, OrganizationResourceAccess])`). Resolve org via `_get_org(info)`; fetch `CalendarEvent.objects.filter_by_organization(org.id).filter(id=event_id)` with the Phase 2 prefetch set; apply `scoped_calendar_ids` owner-scoping exactly as @public_api/queries.py:349-358; return `None` if absent/out-of-scope, else `CalendarEventICSService().build_ics(event).decode("utf-8")`.
2. @public_api/permissions.py: add `"eventIcs": PublicAPIResources.CALENDAR_EVENT` to `OrganizationResourceAccess.FIELD_TO_RESOURCE_MAPPING`.
3. Regenerate the public GraphQL schema artifact if one is checked in (mirror how `calendarEvents` is reflected; check for a committed `schema-auth.yml` / GraphQL SDL snapshot).

Spec use-case: **GraphQL ICS export** — fetch a single event's ICS string via the public bearer-token GraphQL API.

Tests:
- **Integration**: @public_api/tests (alongside the existing `calendar_events` query tests) — token with `CALENDAR_EVENT` resource gets the ICS string for an in-scope event; an org-wide token and a calendar-scoped token both behave like `calendarEvents` (scoped token only sees its calendars' events, else `null`); token lacking the resource is rejected by `OrganizationResourceAccess`; unknown / out-of-org `eventId` returns `null` (no existence leak).

**Suggested AI model**: Tier 3 — `claude-sonnet-4-6` / `gpt-5` / `gemini-2.5-pro`. Owner-scoping correctness + permission-mapping are security-sensitive; worth the stronger tier even though the diff is small.

**Reusable skills**: `create-graphql-public-query` (field registration + resource mapping + auth classes); `write-tests`.

Acceptance: `eventIcs(eventId: <in-scope id>)` returns a valid ICS string for an authorized token; a calendar-scoped token returns `null` for an event outside its calendars; a token without the `CALENDAR_EVENT` resource is denied; unknown id returns `null`.

---

> **No flag-removal phase**: no feature flag is introduced (see **Guiding Decisions** → *No feature flag*), so the mandatory removal phase does not apply.

## 6. Risk & Rollout Notes

- **Feature flag**: none. Justification recorded in **Guiding Decisions** — purely additive read-only REST action + public query; no existing flow, route, or response shape changes; repo has no flag mechanism to extend.
- **New dependency**: `icalendar` is added in Phase 1. Risk is low (mature, widely-used, MIT). Pin a compatible range in `pyproject.toml` and refresh `uv.lock`. Verify it imports cleanly in CI before later phases depend on it. Rollback = revert the dependency + the single service file; nothing else references it until Phase 3/4.
- **Migrations / locks / partitions**: none — no schema changes, no DB writes, no backfill.
- **N+1 risk**: the builder reads attendee + recurrence relations. Phases 3 and 4 must apply the prefetch set documented in Phase 2; the integration tests should assert query counts (or use `assertNumQueries`) so a regression to per-attendee queries is caught.
- **PII exposure**: `ATTENDEE`/`ORGANIZER` lines embed participant emails in the downloadable file. This matches existing read access to the event and was explicitly accepted; note it in the PR description so reviewers see it was a deliberate choice.
- **Rollback story**: each surface phase is independently revertible. Reverting Phase 4 leaves REST intact; reverting Phase 3 leaves GraphQL intact; reverting Phases 1–2 is only safe once Phases 3–4 (their only callers) are also reverted — land/remove in dependency order.
- **Schema artifacts**: Phase 3 regenerates `schema.yml` (drf-spectacular); Phase 4 regenerates any committed public GraphQL SDL. Confirm both are committed so CI schema-drift checks pass.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| `SEQUENCE` source — is there a per-event monotonic counter, or derive from `updated_at`/`modified` epoch? | Derive a stable integer from the event's last-modified timestamp; `0` if no such field exists. Confirm a `modified`/`updated_at` field on `CalendarEvent` during Phase 1. | Eng |
| `ORGANIZER` identity — which email represents the organizer (calendar owner's membership email? a fixed no-reply?) for events with multiple owners? | Use the calendar's primary owner membership email; fall back to omitting `ORGANIZER` if none resolvable (attendees still valid). | Product + Eng |
| `UID` namespace for synthetic ids — per-organization domain or a fixed product namespace (`@vinta-schedule`)? | Fixed product namespace `event-{id}@vinta-schedule` for determinism; revisit if multi-tenant UID collisions matter to clients. | Eng |
| Should `EXDATE` also include *modified* (not just cancelled) exceptions, given v1 emits a single VEVENT? | v1: `EXDATE` for cancelled only; modified-occurrence detail is lost (acceptable under single-VEVENT non-goal). Revisit if clients need per-occurrence overrides. | Product |

## 8. Touch List

**Phase 1 — dependency + core builder**
- Edit `pyproject.toml` — add `icalendar`; refresh `uv.lock`.
- New `@calendar_integration/services/ics_service.py` — `CalendarEventICSService.build_ics`.
- Edit @calendar_integration/services/__init__.py — export the service.
- New `@calendar_integration/tests/test_ics_service.py` — builder unit tests.

**Phase 2 — participants + recurrence**
- Edit `@calendar_integration/services/ics_service.py` — `ORGANIZER`/`ATTENDEE`/`RRULE`/`EXDATE`/cancelled-status.
- Edit `@calendar_integration/tests/test_ics_service.py` — recurrence + attendee tests.

**Phase 3 — REST action**
- Edit @calendar_integration/views.py — `download_ics` `@action` on `CalendarEventViewSet` (line 848).
- Edit `schema.yml` — regenerated (drf-spectacular).
- Edit the event-viewset test module under `@calendar_integration/tests/` — REST integration tests.

**Phase 4 — public GraphQL field**
- Edit @public_api/queries.py — `event_ics` query field.
- Edit @public_api/permissions.py — `FIELD_TO_RESOURCE_MAPPING["eventIcs"]`.
- Edit committed public GraphQL SDL artifact (if any) — regenerated.
- Edit the public-API query test module under `@public_api/tests/` — GraphQL integration tests.
