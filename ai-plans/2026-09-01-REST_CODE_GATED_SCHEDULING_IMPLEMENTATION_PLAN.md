# REST Code-Gated Scheduling — Implementation Plan

## 1. Goals

1. Give the REST API the same **unauthenticated, code-gated scheduling writes** the GraphQL API has today: book on a single calendar, book through a calendar group, reschedule (single and group), and cancel — each authorized solely by a single-use booking code.
2. Give the REST API the same **codeless public group booking** path GraphQL has via `createCalendarGroupEvent`: booking through a `CalendarGroup` whose `accepts_public_scheduling` is `True`, with no code and no auth.
3. Give the REST API the same **code-gated reads** GraphQL has: available times, availability windows, unavailable windows, calendar bookable slots, group bookable slots, and group range availability — all repeatable, none consuming the code.
4. Give the REST API a **booking-code minting and revocation** surface for first-party frontends, authenticated with session/JWT + organization membership, authorized owner-or-org-admin, with the mint attributable to the internal user who performed it.
5. Keep the two surfaces' **security posture identical**: writes discriminate failures through the existing `BookingCodeErrorCode` vocabulary; reads disclose nothing about a code's state.
6. Give a patient who booked a **self-service path to reschedule or cancel** their own appointment,
   without an organization admin minting a code for them by hand.
7. Let an organization **read and set `accepts_public_scheduling`** on a `CalendarGroup` over REST,
   and let anyone **discover slots on a public group without a code**, so a codeless booking UI is
   buildable rather than blind.
8. Let a booking code **pin the event duration**, so a patient handed a 30-minute code cannot book, reschedule into, or shop for a longer slot by editing a request parameter. Enforced in the permission service, so every surface that already honours booking codes inherits it.

**Non-goals:**

- **Codeless single-calendar booking.** [`can_perform_scheduling`](../calendar_integration/services/calendar_permission_service.py#L400-L420) honours `calendar.accepts_public_scheduling`, but no unauthenticated GraphQL mutation exposes it — only the code path reaches that short-circuit. REST mirrors that exactly. Explicitly re-confirmed during planning.
- **Slot re-selection on group reschedule.** GraphQL's `rescheduleCalendarGroupEventWithCode` deliberately changes only the event times and preserves existing slot/calendar selections. REST inherits that limitation verbatim.
- **Public-API system-user token auth on the minting endpoints.** GraphQL minting stays the path for integrations; REST minting serves first-party frontends only.
- **Rate limiting / throttling on the unauthenticated surface.** Deliberately deferred — see **Risk & Rollout Notes**.
- **Retiring or changing the existing `public/organizations/<id>/events/` management-token REST surface** in [token_views.py](../calendar_integration/token_views.py). It stays exactly as it is.
- **Any change to the GraphQL schema.** No mutation, query, input, or enum is added, removed, or altered. Note that GraphQL *behavior* does change in one respect: because duration pinning lands in the permission service (Goal 6), the existing `*WithCode` mutations inherit the constraint. This is deliberate — enforcing the pin only in the REST views would leave it bypassable by presenting the same code to GraphQL, which would make the feature worthless. It is backward-compatible by construction: no code in existence pins a duration until Phase 6 can mint one.

## 2. Guiding Decisions

| Decision | Resolution |
|---|---|
| **Code transport** | `X-Booking-Code` request header, on both reads and writes. A header works uniformly for `GET` reads and `POST` writes (a body field would force a query param on reads, splitting the contract), and a distinct header name keeps booking codes visibly separate from the `Authorization: Bearer <base64 management token>` already in use at `public/`. Absence of the header means "codeless". |
| **Mount point** | New `public/booking/*` namespace, wired through a new `calendar_integration/booking_urls.py` mirroring the existing [token_urls.py](../calendar_integration/token_urls.py) / [webhook_urls.py](../calendar_integration/webhook_urls.py) pattern. **No `organization_id` in any path** — the organization is derived from the code (or, for codeless group booking, from the group), never from client input, so a client cannot address a tenant it was not handed a key to. |
| **Error contract (writes)** | Real HTTP statuses plus `{"error_code": ..., "detail": ...}`, reusing the existing [`BookingCodeErrorCode`](../calendar_integration/graphql.py#L944-L968) values so a client can branch identically on either surface. Status map in **API Design**. |
| **Error contract (reads)** | A single opaque `403 {"detail": "Invalid or expired code."}` for *every* code failure — invalid, expired, used, revoked, or wrong-scope. This mirrors [`_CODE_GATED_ERROR_MESSAGE`](../public_api/queries.py#L92-L94) and is deliberate: the reads are cheap and unauthenticated, so discriminating error codes there would turn them into a free oracle for probing code state. The asymmetry with writes is intentional, not an oversight. |
| **Range validation ordering** | The `MAX_CODE_GATED_RANGE` / backwards-range check runs **before** the code is resolved, exactly as [`_validate_code_gated_range`](../public_api/queries.py#L276-L286) does. A `400` for a bad range must be reachable without a valid code, otherwise response timing and status become a second oracle. |
| **Minting auth** | Session/JWT + active organization membership, in an authenticated viewset registered in [routes.py](../calendar_integration/routes.py) alongside the other internal viewsets. |
| **Minting authorization** | Owner-or-org-admin. An org admin may mint for any calendar or group in the organization; a non-admin member may mint only for a calendar they own (`CalendarOwnership`) or a group they participate in. This is the same split [`CalendarGroupPermission`](../calendar_integration/permissions.py#L157-L232) already draws between "manage" (admin) and "view / book" (participating member). |
| **Mint attribution** | New nullable `minted_by_membership` `OrganizationMembershipForeignKey` on `CalendarManagementToken`. [`create_booking_token`](../calendar_integration/services/calendar_permission_service.py#L707-L786) writes `minted_by` into the `minted_by_system_user` FK, which only accepts a `SystemUser`; a REST caller is a `User`. A dedicated column makes REST mints queryable rather than only reconstructible from the audit log. |
| **Duration pinning — storage** | New nullable `duration` `DurationField` on `CalendarManagementToken`. A Postgres `interval` maps straight onto the `datetime.timedelta` every service in this area already speaks (`find_bookable_slots(duration=...)`), so nothing converts at a boundary. Null means unpinned — the behavior every existing code keeps. |
| **Duration pinning — enforcement site** | `CalendarPermissionService.can_perform_scheduling` and `can_perform_update`. Both already receive the proposed start/end times and hold `self.token`, and both already return a bool that upstream turns into `PermissionDenied` → `NOT_PERMITTED`. Enforcing here means REST, GraphQL, and the legacy `public/organizations/<id>/events/` management-token surface all inherit the pin from one check, with the same error code, and no mutation edits. Enforcing in the REST views alone would leave the pin bypassable by replaying the same code against GraphQL. |
| **Duration pinning — writes** | `end_time` stays required, and a request whose `start_time`/`end_time` span differs from the pinned duration is rejected with `403 NOT_PERMITTED`. The server never derives or silently rewrites `end_time`; a booking is only ever created at times the client actually asked for. The views pre-check the span purely so the message can name the pinned duration — the service check behind it is the guarantee. |
| **Duration pinning — reads** | On the two code-gated bookable-slots endpoints, a pinned duration **silently overrides** the client's `duration_seconds`. A `400` naming the mismatch would turn those endpoints into an oracle for whether a code pins a duration and what it is, which the read design avoids everywhere else. Silent override also keeps a patient from browsing slots at a length they cannot actually book. The returned proposals' own spans are how a client legitimately discovers the pinned duration. |
| **Public group addressing** | Codeless booking addresses a `CalendarGroup` by an opaque, non-sequential `public_booking_slug`, never by its integer primary key. Added in Phase 3b after review found that the integer-keyed route was a cross-tenant enumeration oracle: with no `organization_id` anywhere in the path, an anonymous caller could walk `group_id` 1..N and learn from the 404/403/201 split which groups exist in *any* organization and which accept public scheduling. GraphQL's `createCalendarGroupEvent` requires both `organization_id` and `group_id`, so the integer-keyed REST route was a strictly larger probing surface than its own parity target. Throttling was considered and declined; an unguessable identifier removes the oracle outright rather than rate-limiting it. |
| **Slug covers both branches** | The slug replaces the integer id for coded *and* codeless requests on that route, so the integer PK leaves the public surface entirely. The coded branch still validates the addressed group against the token's group and still answers `403`, never `404`, on a mismatch — that guarantee predates the slug and must survive it. |
| **Patient management codes** | A successful booking mints a `RESCHEDULE` and a `CANCEL` code bound to the new event and returns both plaintexts **once**, in the create response. A successful reschedule re-issues a fresh pair for the same event, so the chain continues; a cancel ends it. This is what makes the feature usable: without it a patient can book and then never change anything, because minting is owner-or-org-admin and nothing else issues codes. Note the pre-existing external-attendee token (`[UPDATE_SELF_RSVP, RESCHEDULE, CANCEL]`, minted inside `create_event`) is **not** this mechanism — its plaintext is discarded at mint, so that row has never been usable by anyone. |
| **Explicit booking-code kind** | `CalendarManagementToken` gains an explicit kind discriminator, replacing Phase 6's `minted_by_* IS NOT NULL` heuristic. Forced by the decision above: a management code minted during a **codeless** booking has no user and no system user, so under the heuristic it would be silently un-revokable and `revoke_token` would refuse it — a leaked patient link that could never be killed. The heuristic was recorded as a known fragility at Phase 6 close-out; this is that fragility becoming load-bearing. |
| **Public-group flag over REST** | `accepts_public_scheduling` is exposed on `CalendarGroupSerializer` under its own name (not GraphQL's inverted `is_private`), readable by any member who can see the group and writable only by an org admin — flipping a group public opens codeless booking, the same class of act as creating the group, which `can_manage_calendar_group` already restricts to admins. |
| **Codeless discovery is group-aggregated** | The codeless public-group reads never attribute an interval to a specific member calendar. Bookable slots and range availability come from the group-aware service methods; the availability/unavailable **window** reads, which are calendar-scoped everywhere else, are exposed for a group only in aggregate ("the group has availability here"). A per-calendar breakdown on an unauthenticated endpoint would re-leak which practitioner is which — exactly the disclosure Phase 5's BLOCKER closed. `available_calendar_ids` on the range-availability read is retained, because group booking's `slot_selections` genuinely requires those ids. |
| **Mint endpoint shape** | One `POST /booking-codes/` taking `purpose` (`book` / `reschedule` / `cancel`) plus exactly one of `calendar` / `calendar_group`, plus `event` for the reschedule and cancel purposes. This collapses GraphQL's six `create*BookingCode` mutations into one resource without changing what can be minted — the six combinations are the cross product of two fields. |
| **Reschedule endpoint decomposition** | Two endpoints (single and group), mirroring GraphQL's two mutations, rather than one that dispatches on token scope. The cross-routing `NOT_PERMITTED` responses ("this code is scoped to a calendar group; use the group endpoint") are part of the GraphQL contract clients already handle. See **Open Questions** for the collapse alternative. |
| **No feature flag — purely additive surface** | Every endpoint lives at a brand-new path served by brand-new viewsets. No existing route, serializer, permission class, or query path changes behavior; the one schema change is a new nullable column no existing code reads or writes. The repo has no feature-flag mechanism today, and standing one up to gate a surface nothing currently depends on would be larger than the feature. Rollback is reverting the route registration — see **Risk & Rollout Notes**. |
| **OpenAPI** | Full drf-spectacular annotations; the endpoints land in `schema.yml` via `make` (`manage.py spectacular --file schema.yml`). They are unauthenticated, so documenting them discloses nothing that probing would not. |
| **Phase granularity** | Bundled by concern (seven phases), not one-per-use-case. |

## 3. Data Model Changes

### 3.1 `CalendarManagementToken.minted_by_membership`

In [@calendar_integration/models.py](../calendar_integration/models.py#L2009-L2016), alongside the existing `minted_by_system_user`:

```python
minted_by_membership = OrganizationMembershipForeignKey(
    on_delete=models.SET_NULL,
    related_name="minted_booking_codes",
    null=True,
    blank=True,
    help_text=(
        "The organization member who minted this booking code through the "
        "authenticated REST surface, if any. Null for codes minted by a "
        "SystemUser (see minted_by_system_user) or by internal flows."
    ),
)
if TYPE_CHECKING:
    minted_by_membership_user_id: int | None
```

`OrganizationMembershipForeignKey` ([common/fields.py:147](../common/fields.py#L147)) contributes a concrete `minted_by_membership_user_id` `BigIntegerField` plus a `ForeignObject` descriptor joining `(organization_id, minted_by_membership_user_id)` → `OrganizationMembership(organization_id, user_id)`. Per that field's own contract, the adopting migration **must** add:

1. A composite index on `(organization, minted_by_membership_user_id)` — the field deliberately adds no single-column index, because every tenant-scoped query filters organization-first. Name it `calmgmttoken_org_minter_idx`, matching the existing `calmgmttoken_org_member_idx` convention.
2. A raw-SQL composite FK constraint — the `ForeignObject` carries none.

**On-delete semantics.** The existing membership FKs in this app ([0026](../calendar_integration/migrations/0026_calendarownership_membership_protect_fk.py), [0032](../calendar_integration/migrations/0032_eventattendance_membership_protect_fk.py)) implement PROTECT as `ON DELETE NO ACTION DEFERRABLE INITIALLY DEFERRED`. PROTECT is wrong here: it would block deleting any user who has ever minted a code. This column needs SET-NULL semantics, matching `minted_by_system_user`. A bare composite `ON DELETE SET NULL` would null **both** referenced columns, including the NOT NULL tenant key `organization_id` — unacceptable. The project runs Postgres 18 (`postgres:alpine`, `/var/lib/postgresql/18/`), so use the column-list form:

```sql
ALTER TABLE calendar_integration_calendarmanagementtoken
  ADD CONSTRAINT calmgmttoken_minter_membership_fk
  FOREIGN KEY (minted_by_membership_user_id, organization_id)
  REFERENCES organizations_organizationmembership (user_id, organization_id)
  ON DELETE SET NULL (minted_by_membership_user_id)
  DEFERRABLE INITIALLY DEFERRED
  NOT VALID;
```

Added `NOT VALID` then `VALIDATE CONSTRAINT` in a second operation with `atomic = False`, exactly as `0026` does. `DEFERRABLE INITIALLY DEFERRED` for the same reason `0026` documents: Django's cascade collector is blind to `ForeignObject` dependencies during organization deletion, so a non-deferrable constraint would abort org deletion depending on collector ordering.

### 3.2 `CalendarManagementToken.duration`

In [@calendar_integration/models.py](../calendar_integration/models.py#L2004-L2008), alongside `expires_at`:

```python
duration = models.DurationField(
    null=True,
    blank=True,
    help_text=(
        "When set, the event booked or rescheduled with this code must span "
        "exactly this duration. Enforced by CalendarPermissionService, so every "
        "surface that honours booking codes inherits it. Null means unpinned."
    ),
)
```

A plain nullable column with no default and no index — nothing filters on it, every read that needs it already has the token row in hand. No composite FK, no `NOT VALID` dance; it rides along in the same migration as `minted_by_membership` because it lands on the same table and adding two nullable columns in one `ALTER TABLE` is one metadata-only operation instead of two.

**Enforcement**, per **Guiding Decisions**:

- [`can_perform_scheduling`](../calendar_integration/services/calendar_permission_service.py#L400-L442) — the pin must be checked **before** the `accepts_public_scheduling` short-circuit at the top of that method. That clause returns `True` without touching the token, so a pinned code presented against a publicly-schedulable calendar would otherwise skip the check entirely. The rule is: when a token is present and pins a duration, a span mismatch is `False` regardless of anything else; only then does the existing authorization logic run.
- [`can_perform_update`](../calendar_integration/services/calendar_permission_service.py#L379-L397) — same check against `new_event`'s span, skipped when `new_event is None` (cancellation has no duration).
- **The group booking path needs verification.** `CalendarGroupService.create_grouped_event` is expected to reach `can_perform_scheduling` per member calendar, which would give group booking the pin for free. If it does not, add a times-aware overload of [`can_perform_group_scheduling`](../calendar_integration/services/calendar_permission_service.py#L444-L474) — that method currently takes only the group and so cannot see a duration. Confirm before implementing Phase 2; do not assume.

### 3.3 `create_booking_token` signature

[`CalendarPermissionService.create_booking_token`](../calendar_integration/services/calendar_permission_service.py#L707) gains two keyword-only parameters:

```python
def create_booking_token(
    self,
    organization_id: int,
    permissions: list[EventManagementPermissions],
    expires_at: datetime.datetime | None = None,
    minted_by: "SystemUser | None" = None,
    minted_by_user: "User | None" = None,          # NEW
    duration: datetime.timedelta | None = None,    # NEW
    calendar_id: int | None = None,
    calendar_group_id: int | None = None,
    event_id: int | None = None,
) -> tuple[CalendarManagementToken, str]:
```

When `minted_by_user` is supplied it sets `token.minted_by_membership_user_id`, and it becomes the audit actor passed to `actor_from_user_or_token` — which [already handles `User`](../audit_integration/services.py#L199-L200), so no audit-side change is needed. `minted_by` and `minted_by_user` are mutually exclusive; supplying both raises `ValueError`. `duration`, when supplied, must be strictly positive; zero or negative raises `ValueError` rather than minting a code that can never be used.

Both parameters default to `None`. Every existing call site passes neither, so behavior is unchanged for all of them.

## 4. API Design

### 4.1 Unauthenticated code-gated writes — `public/booking/`

All accept `X-Booking-Code: <plaintext code>`. All return `201 Created` with a `CalendarEventSerializer` body on success (cancel returns `204 No Content`).

| Method + path | Mirrors | Code scope required |
|---|---|---|
| `POST /public/booking/calendar-events/` | `createCalendarEventWithCode` | `CREATE`, calendar-scoped, not group-scoped |
| `POST /public/booking/calendar-groups/<group_id>/events/` | `createCalendarGroupEventWithCode` **and** `createCalendarGroupEvent` | `CREATE`, group-scoped — **or no code** when the group accepts public scheduling |
| `POST /public/booking/events/reschedule/` | `rescheduleCalendarEventWithCode` | `RESCHEDULE`, event-scoped, not group-scoped |
| `POST /public/booking/group-events/reschedule/` | `rescheduleCalendarGroupEventWithCode` | `RESCHEDULE`, event-scoped **and** group-scoped |
| `POST /public/booking/events/cancel/` | `cancelEventWithCode` | `CANCEL`, event-scoped; dispatches on whether the token is group-scoped |

Request bodies mirror the corresponding strawberry inputs in [@calendar_integration/mutations.py](../calendar_integration/mutations.py#L556-L631), minus the `code` field (now a header):

```jsonc
// POST /public/booking/calendar-events/
{
  "title": "Consultation",
  "description": "",                       // optional, defaults to ""
  "start_time": "2026-09-10T14:00:00Z",
  "end_time": "2026-09-10T14:30:00Z",
  "timezone": "America/Sao_Paulo",
  "external_attendee": {"email": "a@b.com", "name": "A B"}
}

// POST /public/booking/calendar-groups/<group_id>/events/
{ ...as above, plus:
  "slot_selections": [{"slot_id": 1, "calendar_ids": [7, 9]}]
}

// POST /public/booking/events/reschedule/  and  /group-events/reschedule/
{
  "start_time": "...", "end_time": "...", "timezone": "America/Sao_Paulo"
}

// POST /public/booking/events/cancel/   — empty body
```

**Pinned duration.** When the resolved code carries a `duration`, a request whose `end_time - start_time` differs from it is rejected:

```json
{"error_code": "NOT_PERMITTED", "detail": "This code is fixed to a 30 minute booking."}
```

`end_time` is never derived or rewritten — the event is only ever created at the times the client sent. This applies to all four write endpoints that carry times (both creates, both reschedules); cancel has no times to pin. A client discovers the pinned duration from the spans of the proposals returned by the bookable-slots reads.

`calendar_id`, `event_id`, and (for coded group booking) `group_id` come **strictly from the resolved token**, never from the request, exactly as the GraphQL mutations enforce. On the group-booking endpoint the path `<group_id>` is a routing convenience: when a code is present it must equal the token's group, otherwise the response is `403 NOT_PERMITTED` (never a 404, which would confirm the code's real group).

**Write error status map:**

| `error_code` | HTTP | Meaning |
|---|---|---|
| `INVALID_CODE` | `404` | Unknown, malformed, or wrong-organization code |
| `NOT_PERMITTED` | `403` | Code is live but lacks the permission, or is scoped to the wrong target |
| `REVOKED` | `403` | Explicitly revoked by the minting organization |
| `EXPIRED` | `410` | `expires_at` has passed |
| `ALREADY_USED` | `409` | Consumed by a prior successful write |
| `SLOT_UNAVAILABLE` | `409` | Slot taken or policy-violating; the code is **not** consumed and may be retried |

`OverLimitError` continues to render as `402` through the existing [`vinta_exception_handler`](../common/exception_handlers.py) — it is not a booking-code outcome and must not be swallowed into this vocabulary. Body shape:

```json
{"error_code": "ALREADY_USED", "detail": "This booking code has already been used."}
```

### 4.2 Unauthenticated code-gated reads — `public/booking/`

All require `X-Booking-Code`. All are repeatable and never consume the code. Every code failure is `403 {"detail": "Invalid or expired code."}` with no `error_code`.

| Method + path | Query / body | Mirrors | Response |
|---|---|---|---|
| `GET /public/booking/available-times/` | `start_datetime`, `end_datetime` | `availableTimesWithCode` | `AvailableTimeSerializer(many=True)` |
| `GET /public/booking/availability-windows/` | `start_datetime`, `end_datetime` | `availabilityWindowsWithCode` | `AvailableTimeWindowSerializer(many=True)` |
| `GET /public/booking/unavailable-windows/` | `start_datetime`, `end_datetime` | `unavailableWindowsWithCode` | `UnavailableTimeWindowSerializer(many=True)` |
| `GET /public/booking/calendar-bookable-slots/` | `search_window_start`, `search_window_end`, `duration_seconds`, `slot_step_seconds` (default 900) | `calendarBookableSlotsWithCode` | `BookableSlotProposalSerializer(many=True)` |
| `GET /public/booking/calendar-group-bookable-slots/` | same as above | `calendarGroupBookableSlotsWithCode` | `BookableSlotProposalSerializer(many=True)` |
| `POST /public/booking/calendar-group-availability/` | body `{"ranges": [{"start_time": ..., "end_time": ...}]}` | `calendarGroupAvailabilityWithCode` | `CalendarGroupRangeAvailabilitySerializer(many=True)` |

Group availability is a `POST` because it takes a list of ranges — matching the existing authenticated [`CalendarGroupViewSet.availability`](../calendar_integration/views.py#L2463-L2496) action, which is also a `POST` for the same reason. Every response serializer above already exists in [@calendar_integration/serializers.py](../calendar_integration/serializers.py); none needs to be written.

Scope resolution mirrors the queries exactly: calendar-scoped reads take `token.calendar`, falling back to `token.event.calendar`; group-scoped reads take `token.calendar_group`, falling back to `token.event.calendar_group`. A code that resolves to neither yields the uniform `403`. `calendar-bookable-slots` rejects group-scoped codes (single/bundle only) — also via the uniform `403`.

Range validation returns `400 {"detail": "Invalid time range."}` or `400 {"detail": "Requested time range is too large."}`, checked **before** the code is resolved.

**Pinned duration on the two bookable-slots reads.** When the resolved code carries a `duration`, it replaces the client's `duration_seconds` and the parameter becomes optional. No error, no warning — a mismatch is not distinguishable from a match, so these endpoints cannot be used to test whether a code pins a duration. The returned proposals then span exactly the pinned duration, which is how a client legitimately learns what it is before calling a write endpoint that will reject any other span.

### 4.3 Authenticated minting — `booking-codes`

Registered in [routes.py](../calendar_integration/routes.py) as `basename="BookingCodes"`. Session/JWT + active organization membership; owner-or-org-admin authorization.

```jsonc
// POST /booking-codes/
{
  "purpose": "book",              // "book" | "reschedule" | "cancel"
  "calendar": 12,                 // exactly one of calendar / calendar_group
  "calendar_group": null,
  "event": null,                  // required when purpose is reschedule/cancel; forbidden for book
  "expires_at": "2026-09-30T00:00:00Z",  // optional
  "duration_seconds": 1800        // optional; pins the event length, forbidden for purpose=cancel
}

// 201 Created — the plaintext code is returned exactly once and never stored
{
  "id": 4471,
  "code": "NDQ3MTp4Y2s5...",
  "purpose": "book",
  "calendar": 12,
  "calendar_group": null,
  "event": null,
  "expires_at": "2026-09-30T00:00:00Z",
  "duration_seconds": 1800
}
```

`purpose` maps to permissions: `book` → `[CREATE]`, `reschedule` → `[RESCHEDULE]`, `cancel` → `[CANCEL]` — matching what the six GraphQL mint mutations grant.

`duration_seconds` is accepted as an integer at the API boundary (matching the `duration_seconds` the bookable-slots reads already use) and stored as a `timedelta`. It must be strictly positive. It is rejected for `purpose=cancel`, which pins nothing. This is the only way to set a pinned duration — the GraphQL mint mutations are not changed, so codes minted there are always unpinned.

```
DELETE /booking-codes/<id>/   → 204 No Content
```

Revoke is **idempotent**: revoking an already-revoked code, or an id that does not exist within the caller's organization, returns `204`. This mirrors `revokeBookingCode`'s idempotent contract and keeps the endpoint from becoming an existence oracle. No `list` or `retrieve` action is exposed — there is nothing safe to return about a code after mint.

## 5. Phased Rollout

### Phase 0 — Booking-code REST scaffolding, mint attribution, and duration pinning

**Goal**: Ship value: none on its own, with one exception — the duration pin becomes enforceable on *every* existing booking-code surface the moment this phase lands, because it goes into the permission service rather than into any one view. Otherwise this lands the shared pieces every later phase consumes: code resolution from a header, the error-rendering vocabulary, the `public/booking/` mount point, and the two new columns — so Phases 1–6 each stay a thin, reviewable endpoint diff instead of re-deriving the same plumbing six times.

**Feature flag**: none — purely additive scaffolding with no reachable behavior (the router is registered with no viewsets yet).

Changes:

1. `@calendar_integration/booking_exceptions.py` (new): a `BookingCodeAPIException(APIException)` base plus one subclass per `BookingCodeErrorCode` value, each carrying its `status_code` per the **API Design** map and rendering `detail` as `{"error_code": ..., "detail": ...}`. DRF's own handler renders a dict `exc.detail` verbatim, so [common/exception_handlers.py](../common/exception_handlers.py) needs **no** change — important, because that module's docstring is explicit that adding a case there can alter transactional semantics. Also add `OpaqueCodeError(PermissionDenied)` rendering the uniform read-side `403 {"detail": "Invalid or expired code."}`.
2. `@calendar_integration/booking_auth.py` (new): `resolve_booking_code_from_request(request, permission_service)` reading `X-Booking-Code`, calling [`resolve_code`](../calendar_integration/services/calendar_permission_service.py#L788), and translating `InvalidTokenError` / `TokenExpiredError` / `TokenAlreadyUsedError` / `TokenRevokedError` into the write-side exceptions from step 1. A sibling `resolve_booking_code_opaquely(...)` collapses all four into `OpaqueCodeError` for the read endpoints. Plus `client_ip_from_request(request)` mirroring [`_client_ip_from_request`](../calendar_integration/mutations.py) for consume auditing, and `MAX_CODE_GATED_RANGE` / `validate_code_gated_range` mirroring [public_api/queries.py:96](../public_api/queries.py#L96) — the constant is re-exported from a shared home so the two surfaces cannot drift apart.
3. `@calendar_integration/booking_views.py` (new): a `BookingCodeViewMixin` holding `authentication_classes = ()`, `permission_classes = ()`, DI wiring for `calendar_permission_service` / `calendar_service` / `calendar_group_service` / `bookable_slots_service`, and the code-resolution helpers. No concrete viewsets yet.
4. `@calendar_integration/booking_urls.py` (new): `app_name = "calendar_booking_api"` plus an empty `DefaultRouter` and `urlpatterns`, mirroring [token_urls.py](../calendar_integration/token_urls.py).
5. [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py#L74): add `path("public/booking/", include("calendar_integration.booking_urls"))` directly after the existing `public/` include.
6. [@calendar_integration/models.py](../calendar_integration/models.py): add `minted_by_membership` and `duration` per **Data Model Changes**, plus the `calmgmttoken_org_minter_idx` composite index in `Meta.indexes`.
7. One migration adding **both** columns, the composite index, and the raw-SQL `ON DELETE SET NULL (minted_by_membership_user_id) DEFERRABLE INITIALLY DEFERRED NOT VALID` constraint followed by `VALIDATE CONSTRAINT`, with `atomic = False`. Model after [0026_calendarownership_membership_protect_fk.py](../calendar_integration/migrations/0026_calendarownership_membership_protect_fk.py), including its lock/downtime audit docstring — but note the on-delete difference and document why SET NULL replaces the PROTECT/NO ACTION used there. `duration` needs none of that apparatus; it is a plain nullable column riding the same `ALTER TABLE`.
8. [@calendar_integration/services/calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py#L707): add `minted_by_user` and `duration` to `create_booking_token` per **Data Model Changes**, raising `ValueError` when both actor kinds are supplied and when `duration` is non-positive.
9. **Duration enforcement** in the same service: `can_perform_scheduling` checks the pin **before** its `accepts_public_scheduling` short-circuit (see **Data Model Changes** for why that ordering is the whole point), and `can_perform_update` checks `new_event`'s span, skipping when `new_event is None`. Add a small shared helper rather than writing the comparison twice. Both keep returning `bool` — no new exception type, no new error code, so every existing caller on every surface renders a pin violation as the `NOT_PERMITTED` it already renders for a permission failure.
10. **Verify the group path** reaches `can_perform_scheduling` via `CalendarGroupService.create_grouped_event`. If it does not, add a times-aware overload of `can_perform_group_scheduling` — it currently takes only the group and cannot see a duration. Resolve this in Phase 0, not Phase 2, so the enforcement story is complete before any endpoint depends on it.
11. `@calendar_integration/booking_auth.py`: `pinned_duration_error(token, start_time, end_time)` returning the view-layer `403 NOT_PERMITTED` with a message naming the pinned duration, or `None`. This exists purely for the error message — the service check behind it is what makes the pin unbypassable, and the two must not drift.

Spec use-case: shared scaffolding, plus duration pinning (Goal 6) across every existing booking-code surface.

Tests:

- **Unit**: `@calendar_integration/tests/test_booking_code_auth.py` — header extraction (present / absent / empty), each service exception mapping to the right status and `error_code`, the opaque read variant collapsing all four to one indistinguishable `403`, and `validate_code_gated_range` rejecting backwards and over-366-day ranges.
- **Unit**: `@calendar_integration/tests/services/test_calendar_permission_service_duration.py` — `can_perform_scheduling` rejects a span mismatch and accepts an exact match; **rejects a mismatch even when the calendar has `accepts_public_scheduling=True`** (the ordering regression test — this is the one that fails if the check is placed after the short-circuit); `can_perform_update` rejects a reschedule to a different span; cancellation (`new_event is None`) is unaffected; a token with `duration=None` behaves byte-identically to today on every one of these.
- **Integration**: `@calendar_integration/tests/test_booking_code_duration_cross_surface.py` — a code pinned to 30 minutes is rejected at 60 minutes through **GraphQL** `createCalendarEventWithCode` and `rescheduleCalendarEventWithCode`, and through the legacy `public/organizations/<id>/events/` management-token surface. This is the test that proves the pin is not bypassable by choosing a different API, which is the entire reason enforcement lives in the service.
- **Integration**: `@calendar_integration/tests/test_management_token_minter_fk.py` — deleting a `User` who minted a code nulls `minted_by_membership_user_id` and leaves the token row live; deleting the whole `Organization` succeeds (proving the deferred constraint does not trip the cascade collector); a token row cannot reference a `(user, organization)` pair with no membership.
- **Integration**: existing `@calendar_integration/tests/test_calendar_permission_service_codes.py` gains a case asserting `create_booking_token` with neither new argument behaves byte-identically to today, so no existing caller changes.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)) for the migration and the permission-service changes — the composite-FK-with-column-list SET NULL has no in-repo precedent, the `OrganizationMembershipForeignKey` contract has three separate requirements that are easy to half-satisfy, and the pin's placement relative to the `accepts_public_scheduling` short-circuit is a correctness detail that reads fine either way. Tier 2 for the exception classes, mixin, and URL wiring.

**Review models**: reviewer Tier 4 — this phase changes an authorization method every booking surface in the codebase already calls, and adds a raw-SQL constraint with delete semantics that differ from both in-repo precedents on a table in the auth path. A pin placed after the public-scheduling short-circuit is silently unenforced on exactly the calendars most likely to be publicly bookable; a wrong on-delete or deferrability is discovered in production during an org deletion, not in CI. Fixer left on the project default.

**Reusable skills**: `add-migration` (couples with the `migration-author` sub-agent for the composite FK and its reverse path).

Acceptance: `GET /public/booking/` resolves (empty router, 404 on any sub-path); `manage.py migrate` applies and reverses cleanly; `pg_constraint` contains `calmgmttoken_minter_membership_fk` as validated; a code with `duration=30min` is refused a 60-minute booking through GraphQL and through the legacy token surface; the full existing suite is green with no changed assertions.

---

### Phase 1 — Code-gated single-calendar booking

**Goal**: An external attendee holding a `CREATE` booking code can book an event on the bound calendar over REST, with no authentication, and the code is consumed exactly once.

**Feature flag**: none — new endpoint at a new path, no existing caller reaches it.

Changes:

1. `@calendar_integration/serializers.py`: add `BookingCodeEventCreateSerializer` (title, description, start_time, end_time, timezone, external_attendee) mirroring `CreateEventWithCodeInput`. Follow the existing `CalendarGroupEventCreateSerializer` shape, including its `validate_end_time` cross-field check.
2. `@calendar_integration/booking_views.py`: `BookingCodeCalendarEventViewSet` with a single `create` action. Port the seven-step flow from [`create_calendar_event_with_code`](../calendar_integration/mutations.py#L1155-L1298) verbatim, including the ordering **create first, then consume**, inside one `transaction.atomic()`. *(Corrected during implementation: the original rationale here was wrong. Inside one atomic block any exception unwinds everything, so the DB outcome is ordering-independent. What create-first actually changes is that both racers reach the write adapter — which is what the concurrency test asserts. Note the real cost: on a provider-backed calendar the losing racer may already have created an event at the external provider before the DB rolls back, leaving an orphan the rollback cannot undo. Pre-existing in the GraphQL original.)*. Reject group-scoped codes with `403 NOT_PERMITTED`.
3. Map the domain exceptions exactly as the mutation does: `BookingPolicyViolationError` and `NoAvailableTimeWindowsError` / `EventManagementError` → `SLOT_UNAVAILABLE` (code **not** consumed, retryable), `PermissionDenied` → `NOT_PERMITTED`, and let `OverLimitError` propagate to the existing `402` handler untouched.
4. Call Phase 0's `pinned_duration_error` before dispatching to the service, so a span mismatch returns a message naming the pinned duration rather than the generic permission failure the service would produce on its own.
5. `@calendar_integration/booking_urls.py`: register `calendar-events`.
6. drf-spectacular annotations; regenerate `schema.yml` via `make`.

Spec use-case: code-gated single-calendar booking (GraphQL parity with `createCalendarEventWithCode`).

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_rest_create_event.py`, porting the seven scenarios documented in [public_api/tests/test_book_with_code.py](../public_api/tests/test_book_with_code.py) — happy path with the code consumed; replay → `409 ALREADY_USED` with no second event; failed write does **not** consume (`409 SLOT_UNAVAILABLE`, code still active, retry succeeds); code without `CREATE` → `403 NOT_PERMITTED`; group-scoped code → `403 NOT_PERMITTED`; expired / revoked / unknown → `410 EXPIRED` / `403 REVOKED` / `404 INVALID_CODE`; and the event lands in the *code's* organization, not any client-supplied one.
- **Integration**: a pinned-duration case — a code with `duration=30min` books at 30 minutes and is refused at 45 with `403 NOT_PERMITTED` and no event created; the refusal leaves the code **unconsumed** (it is an authorization failure, not a spent attempt, so the patient can retry at the right length); a code with `duration=None` accepts any span.
- **Integration**: a concurrency case driving two simultaneous requests with one code and asserting exactly one event exists afterwards.
- **Integration**: extend `@calendar_integration/tests/test_event_creation_surfaces.py` — that file enumerates every entry point reaching `create_event` and asserts each meters `event_occurrences`. This adds a seventh; leaving it out would let the new surface go unmetered.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). The create-then-consume ordering and the consume-vs-not-consume split across failure kinds are the whole point of the phase, and both are easy to get subtly backwards.

**Reusable skills**: `create-rest-endpoint` (viewset + serializer + route + `schema.yml` regeneration).

Acceptance: `POST /public/booking/calendar-events/` with a valid `CREATE` code and an available slot returns `201` with the event, marks the code used, and meters one `event_occurrences` unit; the same request repeated returns `409 ALREADY_USED` and creates nothing.

---

### Phase 2 — Code-gated group booking

**Goal**: An external attendee holding a group-scoped `CREATE` code can book a grouped event over REST, with the group taken from the code rather than from the request.

**Feature flag**: none — new endpoint at a new path.

Changes:

1. `@calendar_integration/serializers.py`: add `BookingCodeGroupEventCreateSerializer` — the Phase 1 serializer plus `slot_selections`, reusing the existing `_CalendarGroupSlotSelectionInputSerializer`.
2. `@calendar_integration/booking_views.py`: `BookingCodeGroupEventViewSet.create`, porting [`create_calendar_group_event_with_code`](../calendar_integration/mutations.py#L1305-L1486). `group_id` comes strictly from `token.calendar_group`; the path `<group_id>` is validated against it and mismatch is `403 NOT_PERMITTED`, never `404`. Reject single-calendar-scoped codes with `403 NOT_PERMITTED`.
3. Apply `pinned_duration_error` as in Phase 1. *(Resolved during implementation: `create_grouped_event` does **not** reach `can_perform_scheduling` — `CalendarEventService.create_event` skips that gate entirely when `event_data.group_authorized=True`, and `CalendarGroupService` always sets the flag. The pin therefore lives in a times-aware `can_perform_group_scheduling(group, *, start_time, end_time)`, whose only caller is `calendar_group_service.py`. Do not assume the single-calendar gate covers group booking.)*
4. `@calendar_integration/booking_urls.py`: register `calendar-groups/<int:group_id>/events`.
5. drf-spectacular annotations; regenerate `schema.yml`.

Spec use-case: code-gated group booking (GraphQL parity with `createCalendarGroupEventWithCode`).

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_rest_create_group_event.py`, porting [public_api/tests/test_book_group_with_code.py](../public_api/tests/test_book_group_with_code.py) — happy path across multiple slots; single-calendar code rejected; a path `group_id` that differs from the token's group returns `403`, not `404`, and books nothing; replay; non-consumption on slot conflict; expired / revoked / unknown.
- **Integration**: a case proving a code for group A used against `/calendar-groups/<B>/events/` neither books in A nor discloses that A is the real target.
- **Integration**: a group code with a pinned duration is refused at any other span, across a multi-slot selection — the pin applies to the grouped event's own times, not per member calendar.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)) — Phase 1 establishes the pattern; this applies it with a different service call and one extra scope assertion. Step up to Tier 3 if the slot-selection validation proves fiddlier than the existing group serializer suggests.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /public/booking/calendar-groups/<id>/events/` with a matching group-scoped `CREATE` code returns `201` and consumes the code; the same code against a different group id returns `403 NOT_PERMITTED` and creates nothing.

---

### Phase 3 — Codeless public group booking

**Goal**: Anyone can book through a `CalendarGroup` whose `accepts_public_scheduling` is `True` with no code and no authentication, matching GraphQL's `createCalendarGroupEvent`.

**Feature flag**: none — this adds a branch to the Phase 2 endpoint, but that endpoint is itself new and has no callers outside this plan.

Changes:

1. `@calendar_integration/booking_views.py`: in `BookingCodeGroupEventViewSet.create`, branch on the presence of `X-Booking-Code`. Absent → skip code resolution entirely, take the group from the path, and delegate to `CalendarGroupService.create_grouped_event`, which already routes through [`can_perform_group_scheduling`](../calendar_integration/services/calendar_permission_service.py#L444-L474) — that method's first clause is the `accepts_public_scheduling` short-circuit, so the authorization decision stays in the service and is not restated in the view.
2. Map `PermissionServiceInitializationError` → `403 NOT_PERMITTED` with the same message [`create_calendar_group_event`](../calendar_integration/mutations.py#L750-L759) uses ("This group does not accept public scheduling. A token or scheduling code is required."), and `CalendarGroup.DoesNotExist` → `404`.
3. No code is consumed on this path — there is none. Assert this explicitly. For the same reason there is **no pinned duration** on the codeless branch: the pin lives on a code, so a codeless booking is unconstrained in length by design. If a group needs its bookings length-constrained, the mechanism is a booking policy, not `accepts_public_scheduling`.
4. Update the drf-spectacular annotation to document `X-Booking-Code` as optional on this one endpoint; regenerate `schema.yml`.

Spec use-case: codeless public group booking (GraphQL parity with `createCalendarGroupEvent`).

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_rest_codeless_group.py` — a group with `accepts_public_scheduling=True` books with no header; a group with `accepts_public_scheduling=False` returns `403 NOT_PERMITTED` and books nothing; a group that accepts public scheduling *and* is handed a valid group code still books and **does** consume that code (the coded branch wins when the header is present); a codeless booking against a group in another organization is not reachable.
- **Integration**: an explicit assertion that a codeless booking against a public group leaves every `CalendarManagementToken` row in the organization untouched.
- **Integration**: extend `@calendar_integration/tests/test_event_creation_surfaces.py` with the codeless path — it reaches `create_event` too, and must meter.

**Suggested AI model**: Tier 2 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). One branch on an endpoint Phase 2 already built, with the authorization decision delegated to a service method that already implements it.

**Review models**: reviewer Tier 3 — this is the one endpoint in the plan reachable with no credential of any kind, so the review should specifically confirm the `accepts_public_scheduling=False` path cannot be reached by omitting the header.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /public/booking/calendar-groups/<id>/events/` with no `X-Booking-Code` returns `201` when the group has `accepts_public_scheduling=True` and `403 NOT_PERMITTED` when it does not, in both cases without touching any booking code.

---

### Phase 3b — Opaque public slug for codeless group booking

**Goal**: Remove the cross-tenant enumeration oracle Phase 3 introduced, by addressing publicly bookable groups with an unguessable identifier instead of a sequential integer.

**Feature flag**: none — this changes a route added earlier in this same unmerged stack; no deployed caller exists.

Changes:

1. `@calendar_integration/models.py`: add `public_booking_slug` to `CalendarGroup` — a `CharField(max_length=32, unique=True, db_index=True)` whose default is a callable returning `secrets.token_urlsafe(16)` (≈128 bits, 22 URL-safe characters). Uniqueness is **global**, not organization-scoped, because the route carries no organization. Every group gets one, including private groups: the slug alone authorizes nothing, the `accepts_public_scheduling` gate still decides, and a group flipped to public later already has its identifier.
2. Migration, in three operations so no step takes a long lock: add the column nullable with no default; backfill every existing row with a distinct generated slug (idempotent — only fills `NULL`s, so a re-run after a partial failure completes rather than rewriting); then `AlterField` to non-null with the unique constraint, creating the index `CONCURRENTLY` (`atomic = False`). The generator must be collision-checked against existing values rather than assuming uniqueness.
3. [booking_urls.py](../calendar_integration/booking_urls.py): change the group route from `calendar-groups/<int:group_id>/events` to `calendar-groups/<slug:public_slug>/events`.
4. [booking_views.py](../calendar_integration/booking_views.py): resolve the group by `public_booking_slug` on both branches. Codeless still resolves the organization from the resolved group (the `unscoped()` lookup stays, now keyed by an unguessable value). Coded still compares the addressed group against `token.calendar_group_fk_id` and still returns `403 NOT_PERMITTED` on a mismatch, never `404`.
5. [serializers.py](../calendar_integration/serializers.py): expose `public_booking_slug` as a read-only field on `CalendarGroupSerializer`, so an organization admin can read it to build booking links. Read-only — it is never client-settable.
6. Regenerate `schema.yml`.

Spec use-case: hardening of the codeless booking use-case — no new user-facing capability.

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_rest_codeless_group.py` — every existing case re-pointed at the slug route; an unknown slug returns `404`; a well-formed but nonexistent slug is indistinguishable from a malformed one.
- **Integration**: an enumeration-resistance test asserting the old integer-keyed path no longer routes at all, so the oracle is gone rather than merely harder.
- **Integration**: the coded branch still returns `403` (never `404`) when the addressed slug belongs to a group the token was not minted for — the Phase 2 guarantee, re-expressed against slugs.
- **Unit**: slug generation produces distinct values across many calls, and the backfill is idempotent — running it twice leaves the first run's slugs untouched.
- **Integration**: `public_booking_slug` is readable by an org admin through `CalendarGroupSerializer` and is rejected as input on write.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). A three-step migration with a backfill and a concurrent unique index, plus a route change that must preserve an existing security guarantee.

**Review models**: reviewer Tier 4 — this phase exists to close a security hole, and the route change touches the one endpoint reachable with no credential. A reviewer must confirm the oracle is actually gone and that the coded branch's 403-not-404 guarantee survived the rewrite. Fixer left on the project default.

**Reusable skills**: `add-migration` (couples with the `migration-author` sub-agent for the backfill and the concurrent unique index).

Acceptance: `POST /public/booking/calendar-groups/<slug>/events/` books a public group codelessly; the integer-keyed path no longer resolves; every `CalendarGroup` row has a distinct `public_booking_slug`; and a coded request addressing a slug outside its token's group returns `403`, not `404`.


### Phase 4 — Code-gated reschedule and cancel

**Goal**: A holder of a `RESCHEDULE` or `CANCEL` code can move or cancel the exact event it was minted for, over REST, unauthenticated.

**Feature flag**: none — three new endpoints at new paths.

Changes:

1. `@calendar_integration/serializers.py`: add `BookingCodeRescheduleSerializer` (start_time, end_time, timezone). Cancel takes no body.
2. `@calendar_integration/booking_views.py`: three actions porting [`reschedule_calendar_event_with_code`](../calendar_integration/mutations.py#L1487-L1710), [`reschedule_calendar_group_event_with_code`](../calendar_integration/mutations.py#L1711-L1894), and [`cancel_event_with_code`](../calendar_integration/mutations.py#L1895-L1990).
3. Carry over the detail that makes reschedule work: the existing event's title, description, internal attendances, external attendances (including the `ExternalAttendee` id, so status correlation matches), and resource allocations are **snapshotted and replayed unchanged**, with only the time fields overridden. This is what makes `_determine_required_update_permissions` yield exactly `{RESCHEDULE}` — a naive partial update would demand permissions the code does not carry and fail with a misleading `NOT_PERMITTED`.
4. Cross-routing rejections mirror GraphQL: a group-scoped code on `/events/reschedule/` and a single-calendar code on `/group-events/reschedule/` both return `403 NOT_PERMITTED` with a message naming the correct endpoint. A code not bound to a specific event returns `403 NOT_PERMITTED`.
5. `event_id` and `calendar_id` come strictly from the token. Cancel dispatches on `token.calendar_group` exactly as the mutation does. Cancel returns `204 No Content`.
6. Apply `pinned_duration_error` on both reschedule endpoints. Note that a reschedule code's pin constrains the **new** span, not the original event's — a 30-minute pin refuses a move to a 45-minute slot even when the event being moved is currently 45 minutes long. Cancel pins nothing.
7. `@calendar_integration/booking_urls.py`: register `events/reschedule`, `group-events/reschedule`, `events/cancel`. drf-spectacular annotations; regenerate `schema.yml`.

Spec use-case: code-gated reschedule (single and group) and cancel — GraphQL parity with the three corresponding mutations.

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_rest_reschedule.py`, porting [test_reschedule_with_code.py](../public_api/tests/test_reschedule_with_code.py) and [test_reschedule_group_with_code.py](../public_api/tests/test_reschedule_group_with_code.py) — happy path with the code consumed; a `CREATE`-only code rejected; cross-routing both directions; unavailable target slot does not consume; title, description, attendees, and resource allocations are byte-identical after the reschedule.
- **Integration**: `@calendar_integration/tests/test_booking_rest_cancel.py`, porting [test_cancel_with_code.py](../public_api/tests/test_cancel_with_code.py) — single and group cancel, replay after the event is gone, `RESCHEDULE`-only code rejected.
- **Integration**: a code minted for event A cannot touch event B even when both sit on the same calendar and the client tries to say otherwise.
- **Integration**: a `RESCHEDULE` code pinned to 30 minutes refuses a move to a 45-minute span **even when the event is currently 45 minutes long**, and the refusal does not consume the code.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). The preserved-details snapshot is the subtle part — it exists specifically so the permission computation lands on `{RESCHEDULE}`, and reimplementing it from scratch rather than porting it will produce an endpoint that fails only when an event has attendees.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /public/booking/events/reschedule/` with a valid `RESCHEDULE` code moves exactly the bound event, leaves its title / attendees / allocations unchanged, and consumes the code; `POST /public/booking/events/cancel/` with a valid `CANCEL` code returns `204` and deletes exactly that event.

---

### Phase 5 — Code-gated reads

**Goal**: A code holder can browse availability and bookable slots for the calendar or group their code is bound to, repeatably, without consuming it.

**Feature flag**: none — six new read endpoints at new paths.

Changes:

1. `@calendar_integration/booking_views.py`: six actions porting the six code-gated fields in [public_api/queries.py:1405-1640](../public_api/queries.py#L1405-L1640). Every one: validate the range **first**, resolve the code opaquely, resolve scope (calendar or group, with the `token.event` fallback), initialize the service for the token's organization, call the same service method the query calls, serialize.
2. Every failure — invalid, expired, used, revoked, wrong-scope, or a code resolving to neither a calendar nor a group — returns the single `403 {"detail": "Invalid or expired code."}`. No `error_code`, no variation in message, no variation by branch.
3. `calendar-bookable-slots` rejects group-scoped codes; `calendar-group-bookable-slots` and `calendar-group-availability` reject single-calendar codes — all through the same opaque `403`.
4. **Pinned duration** on the two bookable-slots endpoints: when the resolved code carries a `duration`, it replaces `duration_seconds`, and that parameter becomes optional. No error and no warning on a mismatch — a request with the wrong `duration_seconds` must be indistinguishable from one with the right value, or the endpoint becomes an oracle for the pin. `calendar-group-availability` takes explicit ranges rather than a duration, so it is unaffected.
5. Reuse the existing `AvailableTimeSerializer`, `AvailableTimeWindowSerializer`, `UnavailableTimeWindowSerializer`, `BookableSlotProposalSerializer`, `CalendarGroupRangeAvailabilitySerializer`, and `CalendarGroupAvailabilityQuerySerializer` from [@calendar_integration/serializers.py](../calendar_integration/serializers.py) — no new response serializers.
6. Set `pagination_class = None` on the list-shaped reads, matching the existing `available-windows` / `unavailable-windows` actions which return bare arrays.
7. `@calendar_integration/booking_urls.py`: register all six. drf-spectacular annotations with explicit `OpenApiParameter` entries, following the existing [`bookable_slots`](../calendar_integration/views.py#L2498-L2536) annotation; document `duration_seconds` as optional-and-overridable on the two slot endpoints. Regenerate `schema.yml`.
8. Note: `calendar-bookable-slots` has **no** authenticated REST equivalent today — `find_bookable_slots_for_calendar` is reachable only from GraphQL. This is the first REST surface for it, code-gated only.

Spec use-case: code-gated reads — GraphQL parity with the six `*WithCode` query fields.

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_rest_reads.py`, porting [public_api/tests/test_code_gated_reads.py](../public_api/tests/test_code_gated_reads.py) — each endpoint returns correct data for its bound scope; each is repeatable and leaves `used_at` null; each rejects wrong-scope codes.
- **Integration**: an explicit non-disclosure test asserting that invalid, expired, used, revoked, and wrong-scope codes produce **byte-identical** response bodies and the same `403` status on every one of the six endpoints. This is the security property the whole read design exists for; without this test the asymmetry is a comment, not a guarantee.
- **Integration**: range validation returns `400` for a backwards range and for one exceeding 366 days, and does so for an *invalid* code too — proving the range check precedes code resolution and cannot be used to time-probe.
- **Integration**: a code pinned to 30 minutes returns 30-minute proposals whether `duration_seconds` says 1800, says 3600, or is omitted entirely — **byte-identical responses in all three cases**, so the parameter cannot be used to detect the pin. A code with `duration=None` honours `duration_seconds` as before. Pair this with a write assertion: every proposal the pinned read returns is actually bookable through Phase 1, which is the property that makes silent override the right call rather than a trap.

**Suggested AI model**: Tier 2 for the six endpoints (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)) — repetitive application of one pattern against six existing service calls and six existing serializers. Step up to Tier 3 for the non-disclosure test, which has to assert an absence across a matrix rather than a value.

**Review models**: reviewer Tier 3 — the value of this phase is entirely in what it refuses to reveal, and a branch that leaks a distinguishable body reads as correct code.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: all six endpoints return the same payloads their GraphQL counterparts do for a valid code, never set `used_at`, and return one indistinguishable `403` body for every category of code failure.

---

### Phase 6 — Booking-code minting and revocation

**Goal**: A first-party frontend, authenticated as an organization member, can mint a booking / reschedule / cancel code and revoke it, with the mint attributed to the minting user.

**Feature flag**: none — new authenticated endpoint at a new path; no existing viewset or permission class changes.

Changes:

1. `@calendar_integration/serializers.py`: `BookingCodeCreateSerializer` with `purpose`, `calendar`, `calendar_group`, `event`, `expires_at`, `duration_seconds`, and cross-field validation — exactly one of `calendar` / `calendar_group`; `event` required for `reschedule` and `cancel`, forbidden for `book`; `expires_at` must be in the future; `duration_seconds` must be strictly positive and is forbidden for `purpose=cancel`. Plus `BookingCodeCreateResultSerializer` for the one-time response, which must carry `code` as a read-only plaintext field that is never re-derivable.
2. `@calendar_integration/permissions.py`: `BookingCodePermission` implementing owner-or-org-admin. `has_permission` requires an authenticated user with an active membership. Authorization against the specific target happens in the viewset (the target arrives in the body, not as a URL object), delegating to `CalendarOwnership` for calendars and [`can_view_calendar_group`](../calendar_integration/services/calendar_permission_service.py#L507) for groups, with `user.is_organization_admin(...)` as the bypass.
3. `@calendar_integration/views.py`: `BookingCodeViewSet`, a `WriteOnlyVintaScheduleModelViewSet` (or `NoListVintaScheduleModelViewSet` restricted to `create` + `destroy`) exposing only `create` and `destroy`. `create` calls `create_booking_token(..., minted_by_user=request.user, duration=...)`, converting `duration_seconds` to a `timedelta` at the serializer boundary. `destroy` calls [`revoke_token`](../calendar_integration/services/calendar_permission_service.py#L909) and returns `204` even on `InvalidTokenError`, matching `revokeBookingCode`'s idempotent contract.
4. A target in another organization is treated as not found — `404`, never `403` — so the endpoint cannot enumerate calendars or groups across tenants.
5. [routes.py](../calendar_integration/routes.py): register `booking-codes` with `basename="BookingCodes"`. drf-spectacular annotations; regenerate `schema.yml`.

Spec use-case: booking-code minting and revocation — GraphQL parity with the six `create*BookingCode` mutations and `revokeBookingCode`, collapsed into one resource.

Tests:

- **Integration**: `@calendar_integration/tests/test_booking_code_rest_mint.py` — each of the six `purpose` × target combinations mints a code carrying the right `EventManagementPermissions`, and the returned plaintext actually works against the matching Phase 1–4 endpoint (this is the real parity assertion: a REST-minted code must be indistinguishable from a GraphQL-minted one).
- **Integration**: authorization matrix — org admin mints for any calendar or group; a member mints for a calendar they own and for a group they participate in; a member is refused for a calendar they do not own and a group they do not participate in; a cross-organization target returns `404`, not `403`.
- **Integration**: validation matrix — both targets supplied, neither supplied, `event` on `purpose=book`, `event` missing on `purpose=reschedule`, `expires_at` in the past, `duration_seconds` zero / negative, `duration_seconds` on `purpose=cancel`.
- **Integration**: end-to-end duration pinning — minting with `duration_seconds: 1800` produces a code that books at 30 minutes through Phase 1, is refused at 45 with `403 NOT_PERMITTED`, and returns 30-minute proposals from Phase 5's bookable-slots read regardless of the `duration_seconds` sent there. Minting without `duration_seconds` produces a code that accepts any span, exactly as GraphQL-minted codes do.
- **Integration**: `minted_by_membership_user_id` is set to the requesting user and `minted_by_system_user` stays null; the audit entry names the user actor.
- **Integration**: revoke is idempotent (twice → `204`, `204`), a revoked code then fails against a Phase 1 write with `403 REVOKED`, and revoking an id belonging to another organization returns `204` without touching that row.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Cross-field serializer validation driving which service arguments are passed, plus an authorization rule that splits on target kind and admin status, plus the 404-not-403 tenant-isolation choice.

**Review models**: reviewer Tier 4 — this endpoint mints credentials that grant unauthenticated write access to calendar data. An authorization gap here is not a bug in one endpoint; it hands out working keys to every endpoint in Phases 1–4.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: `POST /booking-codes/` as an org admin or calendar owner returns `201` with a plaintext code that successfully books through `POST /public/booking/calendar-events/`; the same request from a non-owning member returns `403`; `DELETE /booking-codes/<id>/` returns `204` twice and renders the code unusable.

---

*No flag-removal phase: this plan declares no feature flag. See the "no flag — purely additive surface" row in **Guiding Decisions** for the justification, and **Risk & Rollout Notes** for the rollback path that replaces it.*

### Phase 7 — Explicit booking-code kind discriminator

**Goal**: Replace Phase 6's `minted_by_* IS NOT NULL` heuristic with an explicit column, so a code minted with no actor is still a revokable booking code. Ship value: none user-visible on its own; it unblocks Phase 8, which mints codes during codeless bookings.

**Feature flag**: none — a new column plus a swap of one queryset predicate; no reachable behavior changes for existing rows.

Changes:

1. `@calendar_integration/models.py`: add `CalendarManagementToken.kind`, a `CharField` with choices (`BOOKING_CODE` / `MANAGEMENT_TOKEN`), non-null, defaulting to `MANAGEMENT_TOKEN` so any path that forgets to set it fails **closed** — un-revokable is better than wrongly-revokable, and the mint paths are few and explicit.
2. Migration: add the column nullable, backfill using the existing heuristic (`minted_by_membership_user_id IS NOT NULL OR minted_by_system_user_id IS NOT NULL` → `BOOKING_CODE`, else `MANAGEMENT_TOKEN`), then set non-null with a DB-level default. Same three-step shape and the same deploy-window reasoning as Phase 3b's `0052`/`0053`/`0054` — Render migrates in `buildCommand`, so old code inserts without the column while the migration is already applied.
3. [querysets.py](../calendar_integration/querysets.py): `booking_codes()` filters on `kind=BOOKING_CODE` instead of the heuristic. Delete the heuristic and the fragility note that documented it.
4. [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py): `create_booking_token` sets `kind=BOOKING_CODE`; every other `create_*_token` sets `MANAGEMENT_TOKEN` explicitly rather than relying on the default.
5. Revert the twelve test fixtures Phase 6 had to amend: they exist only to satisfy the heuristic and should mint with the explicit kind instead.

Spec use-case: shared scaffolding for Phase 8.

Tests:

- **Integration**: a token minted with no actor at all is still classified `BOOKING_CODE` and is revokable — the case the heuristic got wrong.
- **Integration**: owner, attendee, and external-attendee tokens are `MANAGEMENT_TOKEN` and remain un-revokable through both the REST endpoint and `revoke_token`, so Phase 6's privilege-escalation fix still holds.
- **Integration**: the backfill classifies existing rows exactly as the heuristic did — assert over a fixture set covering every `create_*_token` path.
- **Unit**: the migration applies, reverses, and re-applies; an INSERT omitting the column succeeds (deploy-window safety).

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). A three-step migration with a backfill, on a table in the auth path.

**Review models**: reviewer Tier 4 — this column decides what may be revoked. Getting the backfill or the default wrong either resurrects Phase 6's privilege escalation or makes real codes un-revokable. Fixer left on the project default.

**Reusable skills**: `add-migration`.

Acceptance: `booking_codes()` filters on `kind`; an actor-less mint is revokable; owner and attendee tokens are not; the migration applies, reverses, and re-applies; and Phase 6's revoke tests pass unchanged.

---

### Phase 8 — Patient self-service management codes

**Goal**: A patient who books can reschedule or cancel their own appointment without anyone minting a code for them by hand.

**Feature flag**: none — additive fields on responses that already exist in this same unmerged stack.

Changes:

1. [booking_views.py](../calendar_integration/booking_views.py): on a successful create — single-calendar (Phase 1), group coded (Phase 2), and group codeless (Phase 3) — mint a `RESCHEDULE` and a `CANCEL` booking code bound to the new event, scoped to the same calendar or group the booking used, and return both plaintexts in a `management` object on the `201`. Mint inside the existing `transaction.atomic()` so a failed booking issues nothing.
2. Same on a successful reschedule (single and group): re-issue a fresh pair for the event and return them, so the patient can manage it again. Cancel returns `204` and issues nothing — the chain ends.
3. Inherit the booking's pinned duration onto the re-issued reschedule code, so a 30-minute appointment cannot be rescheduled into a 60-minute one by using the code the system itself handed out.
4. Carry `expires_at` from the event rather than leaving it null: a management code for a past appointment is dead weight. Default to the event's end time unless the plan's **Open Questions** resolves otherwise.
5. [serializers.py](../calendar_integration/serializers.py): a small `BookingManagementCodesSerializer` for the `management` object. Write-only in the sense that it is never echoed on a read — these endpoints have no read.
6. Regenerate `schema.yml`.

Spec use-case: patient self-service rescheduling and cancellation.

Tests:

- **Integration**: book, then use the returned `reschedule_code` against `POST /public/booking/events/reschedule/` and confirm it works; then use the *re-issued* code from that response to reschedule again, proving the chain continues.
- **Integration**: book, then use the returned `cancel_code` against `POST /public/booking/events/cancel/` and confirm `204` and deletion.
- **Integration**: the same for a group booking and for a **codeless** group booking — the codeless case is what forces Phase 7's explicit kind, so assert the issued codes are revokable.
- **Integration**: a failed booking (slot unavailable) issues no codes and leaves no token rows.
- **Integration**: a pinned booking's re-issued reschedule code carries the same pin, and refuses a different span.
- **Integration**: the issued codes are scoped to that event only — they cannot touch a second event on the same calendar.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Touches five endpoints' success paths inside their transactions, and the re-issue chain has to preserve scope and pin.

**Review models**: reviewer Tier 4 — this hands a credential to an unauthenticated caller on every booking. Over-scoping one (wrong event, wrong calendar, missing pin) is a privilege leak issued automatically, at volume.

**Reusable skills**: `create-rest-endpoint` (for the schema regeneration).

Acceptance: booking returns working reschedule and cancel codes; rescheduling with one returns a fresh pair; a failed booking returns none; and every issued code is bound to exactly that event and revokable.

---

### Phase 9 — Public-group flag and codeless discovery reads

**Goal**: An organization can read and set `accepts_public_scheduling` over REST, and anyone can discover a public group's availability without a code — so a codeless booking UI can show real slots instead of guessing.

**Feature flag**: none — one serializer field plus new read endpoints at new paths.

Changes:

1. [serializers.py](../calendar_integration/serializers.py): add `accepts_public_scheduling` to `CalendarGroupSerializer`. Readable by any member who can see the group; writable only by an org admin. Enforce the write restriction in the serializer or viewset, not by hoping the caller is an admin — a non-admin PATCH must leave it unchanged and say so.
2. `@calendar_integration/booking_read_views.py`: codeless, slug-addressed public-group reads. All gated on `accepts_public_scheduling`; unknown slug → `404`, non-public group → `403`, mirroring the codeless write's contract from Phase 3:
   - `GET /public/booking/calendar-groups/<slug>/bookable-slots/`
   - `POST /public/booking/calendar-groups/<slug>/availability/` (ranges in the body)
   - `GET /public/booking/calendar-groups/<slug>/availability-windows/` and `.../unavailable-windows/` — **aggregated across the group, never per-calendar** (see the "Codeless discovery is group-aggregated" Guiding Decision). If no coherent non-attributing aggregate exists for a window read, ship the other two and say so rather than leaking a per-calendar breakdown.
3. These are codeless-only and slug-addressed; the Phase 5 code-gated reads stay token-addressed. Do not conflate the two addressing schemes.
4. Regenerate `schema.yml`.

Spec use-case: codeless public-group slot discovery, plus REST management of the flag that enables it.

Tests:

- **Integration**: a public group's slots are readable with no credential; a private group returns `403`; an unknown slug returns `404`.
- **Integration**: the reads return the same data their code-gated Phase 5 counterparts do for the same group.
- **Integration**: every proposal returned is actually bookable through the Phase 3 codeless write — the read and the write agree.
- **Integration**: no response attributes an interval to a specific member calendar on the window reads.
- **Integration**: an org admin can flip `accepts_public_scheduling` and the codeless reads and writes turn on and off accordingly; a non-admin member's attempt leaves it unchanged.

**Suggested AI model**: Tier 3 (IDs in [resources/ai-models.yaml](../.claude/skills/plan-feature/resources/ai-models.yaml)). Four endpoints plus an authorization-sensitive serializer field, and the window aggregation needs a judgement call about what is safe to expose.

**Review models**: reviewer Tier 4 — these are unauthenticated reads on a surface whose whole design constraint is not revealing which calendar is which. Fixer left on the project default.

**Reusable skills**: `create-rest-endpoint`.

Acceptance: an admin can set `accepts_public_scheduling` over REST and a non-admin cannot; a public group's slots, range availability, and aggregated windows are readable with no code; a private group's are not; and no window response names a member calendar.


## 6. Risk & Rollout Notes

**The unauthenticated surface is unthrottled — accepted risk.** Eleven endpoints reachable with no credential are being added, and no rate limiting is planned. Two concrete exposures follow. First, **code brute-forcing**: `resolve_code` decodes a `<id>:<raw>` pair, so an attacker who guesses a token id still needs the long random half, but nothing bounds attempt volume at the app layer. Second, **read amplification**: `calendar-bookable-slots` and `calendar-group-bookable-slots` run slot searches with client-controlled windows up to 366 days and a client-controlled `slot_step_seconds` — a small step over a large window is an expensive query, and `MAX_CODE_GATED_RANGE` bounds the window but not the step. Note that this second one needs only *one* valid code to exploit repeatedly, because reads are deliberately non-consuming.

If this is revisited, the remedy is small and follows a pattern already in the repo: add `booking-code-write` and `booking-code-read` to `DEFAULT_THROTTLE_RATES` in [vinta_schedule_api/settings/base.py](../vinta_schedule_api/settings/base.py#L325-L329) and set `throttle_scope` on the viewsets — the same shape the `payment-webhook` and `payment-provider` scopes already use for unauthenticated endpoints. A cheaper partial mitigation, if full throttling stays out of scope, is clamping `slot_step_seconds` to a floor (say 300s) in Phase 5, which bounds the worst per-request cost without any new infrastructure. Neither is in the plan; both are one-phase additions.

**Duration pinning changes a shared authorization method — Phase 0's real blast radius.** `can_perform_scheduling` and `can_perform_update` are called by every booking surface in the codebase: the REST endpoints this plan adds, all five GraphQL `*WithCode` mutations, the legacy `public/organizations/<id>/events/` management-token viewset, and the internal authenticated flows. That breadth is the point — it is what makes the pin unbypassable — but it also means a mistake there is not scoped to the new surface. Two specific hazards. First, **placement relative to the `accepts_public_scheduling` short-circuit**: that clause returns `True` before reading the token at all, so a pin checked after it is silently unenforced on precisely the calendars most likely to be publicly bookable. Phase 0's ordering test is the regression test for this and must not be dropped. Second, **the null path must be inert**: every code in production has `duration=NULL` until Phase 6 ships, so if the null case is not a clean no-op, this phase breaks every existing booking flow at once, on every surface, with no feature flag to turn it off. Phase 0's "byte-identical to today" assertions carry that guarantee; treat them as the phase's acceptance criterion rather than incidental coverage.

**The codeless route's identifier is security-relevant (Phase 3b).** Phase 3 shipped the codeless branch keyed by `CalendarGroup`'s integer primary key, which — combined with this plan's decision to keep `organization_id` out of every path, and with throttling declined — let an anonymous caller enumerate group ids across every tenant and learn which ones accept public scheduling. Phase 3b replaces that identifier with an unguessable slug. **Phase 3 must not be deployed without Phase 3b.** Merging the stack in order is fine; deploying the codeless surface keyed by an integer is not. If Phase 3b is ever dropped from the plan, throttling stops being optional.

**Migration safety (Phase 0).** The migration adds two nullable columns in one `ALTER TABLE`. Adding a nullable `BigIntegerField` or `interval` with no default is a metadata-only operation in Postgres and takes no table rewrite. The composite index should be created `CONCURRENTLY` if `calendar_integration_calendarmanagementtoken` is large in production — which requires `atomic = False` (already needed for the `NOT VALID` / `VALIDATE` split). The FK constraint is added `NOT VALID` and validated separately, so it takes only a `SHARE UPDATE EXCLUSIVE` lock during validation rather than blocking writes. The `ON DELETE SET NULL (column_list)` syntax requires Postgres 15+; the project runs 18 both locally (`postgres:alpine`, `/var/lib/postgresql/18/`) and on Render — **verify the Render database's actual major version before this migration ships**, because the syntax is a hard parse error on 14 and below, not a degradation.

**Deferred-constraint interaction with organization deletion.** [0026](../calendar_integration/migrations/0026_calendarownership_membership_protect_fk.py) documents at length why membership constraints in this app must be `DEFERRABLE INITIALLY DEFERRED`: Django's cascade collector cannot see `ForeignObject` dependencies and may delete an `OrganizationMembership` before the rows referencing it. The new constraint inherits that requirement. Phase 0's test asserting that whole-organization deletion still succeeds is not optional coverage — it is the regression test for this specific failure mode.

**Rollback.** With no feature flag, rollback is per phase and clean because each phase only adds routes. Reverting a phase's route registration in `booking_urls.py` (or `routes.py` for Phase 6) removes the surface immediately; the viewsets and serializers can stay in the tree harmlessly. The one exception is Phase 0's migration: reverting it drops the constraint, the index, and the column. Codes minted through Phase 6 in the interim keep working in one respect and lose a guarantee in another: `minted_by_membership` is attribution metadata that nothing in the authorization path reads, so dropping it costs only audit detail — but dropping `duration` **silently unpins every code that had a duration**, turning a 30-minute code into an any-length code rather than breaking it. That is a security regression that fails open, so if Phase 0 is ever reverted after Phase 6 has minted pinned codes, revoke those codes first. Revert Phase 6 before Phase 0 regardless, so nothing is writing either column when they disappear.

**Carried forward from implementation — read before extending this surface.**

*Tracked follow-up, pre-existing, not fixed by this plan.* `serialize_event_data_input_util`
(`calendar_integration/services/calendar_service_utils.py`) builds its `resources=[...]` list by
iterating a `Calendar` queryset under a loop variable named `resource_allocation` and then accessing
`.calendar` and `.status` on each item — attributes a `Calendar` row does not have. Rescheduling an
event whose `ResourceAllocation` points at a `calendar_type=RESOURCE` calendar raises
`AttributeError`. It predates this plan and is reachable through the GraphQL reschedule mutations
today, but Phase 4 makes it reachable from an unauthenticated endpoint, where it surfaces as a 500.
Two test suites currently avoid it by not setting that calendar type. It deserves its own focused
change and review.

*Fragility introduced by Phase 6.* The booking-code discriminator is
`minted_by_membership_user_id IS NOT NULL OR minted_by_system_user_id IS NOT NULL`
(`CalendarManagementTokenQuerySet.booking_codes()`), and `revoke_token` resolves through it so that
revoking can never touch a calendar-owner or attendee token. It was verified sound against every
`create_*_token` method. But a future mint path that passes neither actor would produce a booking
code that is silently **un-revokable**. Twelve test files had to be updated for exactly this reason.
If an actor-less mint is ever legitimately needed, replace the heuristic with an explicit kind
column rather than widening the predicate.

*Two slug generators coexist on purpose (Phase 3b).* New rows get
`secrets.token_urlsafe(16)` from Python; the column also carries
`DEFAULT replace(gen_random_uuid()::text, '-', '')`. The DB default exists only because Render runs
`manage.py migrate` inside `buildCommand`, so old-code pods insert without the column while the
migration is already applied — and a service rollback does not revert migrations. Both are
unguessable. Do not unify them.

*Security fixes that also landed on the deployed GraphQL surface.* Two holes found during review
existed identically in already-deployed GraphQL code and were fixed there in the same commits:
scope resolution falling through `token.event.calendar` let a group-scoped code read a specific
calendar's availability (Phase 5), and `revoke_token` accepting any token kind let a caller revoke
calendar-owner tokens, causing permanent lockout (Phase 6). Neither was a schema change.

**Deploy ordering.** No cross-repo dependency. Phase 0 must deploy before any of Phases 1–6. Phases 1, 2, 4, 5, and 6 are independent of each other and can ship in any order once Phase 0 lands. Phase 3 depends on Phase 2 (it adds a branch to that endpoint).

**Metering.** Three phases add entry points that reach `CalendarEventService.create_event` (Phases 1, 2, and 3). [test_event_creation_surfaces.py](../calendar_integration/tests/test_event_creation_surfaces.py) exists precisely because guarding at the viewset layer would leave a path unmetered, and it enumerates every surface. Each of those three phases must extend it. A new booking path that does not meter `event_occurrences` is a billing leak that no other test in the suite would catch.

**`schema.yml` churn.** Six of the seven phases regenerate `schema.yml`. Sequential merges will conflict in that file. Regenerate rather than hand-resolve — `make` runs `manage.py spectacular --color --file schema.yml`. Note also that the repo root holds untracked `.env`, `schema.yml`, and `schema-auth.yml`; stage explicit paths per the project's `git add` policy.

## 7. Open Questions

| Question | Recommended default | Owner |
|---|---|---|
| Should the two reschedule endpoints collapse into one `POST /public/booking/events/reschedule/` that dispatches on the token's scope? The request bodies are already identical, and the split exists only because GraphQL needed two mutation names. Collapsing would drop the two cross-routing `NOT_PERMITTED` responses. | **Keep two endpoints.** The cross-routing errors are part of the contract clients handle today, and a client migrating from GraphQL should not have to discover that one distinction silently disappeared. Revisit if no client ever branches on it. | Eng |
| Should `slot_step_seconds` be clamped to a floor on the two unauthenticated bookable-slot reads? | **Yes, floor at 300s in Phase 5** — it is a two-line change that bounds the worst-case query cost, and it is the only amplification mitigation available given the no-throttling decision. Flagged rather than assumed because it is a behavior difference from the GraphQL counterpart, which has no floor. | Eng |
| Should REST-minted codes also be mintable by public-API system-user tokens later, so integrations get one surface instead of two? | **Not now.** Phase 6's permission class is written against session/JWT + membership; adding token auth later means a second `has_permission` branch and a second authorization path, which is exactly the surface worth reviewing separately rather than bundling. | Product |
| Should the GraphQL mint mutations also accept a duration, so integrations can issue pinned codes? They currently cannot, which means the pin is a REST-minting-only capability even though *enforcement* is universal. | **Not in this plan.** It is a one-field addition to six inputs and is deliberately deferred rather than forgotten — bundling it here would break the "no GraphQL schema change" boundary that keeps this plan reviewable. Raise it as its own follow-up once a real integration asks. | Product |
| Does `minted_by_membership` want a corresponding read surface (e.g. "codes I minted") for first-party frontends? | **No.** Phase 6 deliberately exposes no `list` or `retrieve` — there is nothing safe to return about a code after mint, and the column exists for audit and support queries, not for a client-facing listing. | Product |

## 8. Touch List

**Phase 0 — scaffolding, mint attribution, and duration pinning**

- `@calendar_integration/booking_exceptions.py` (new)
- `@calendar_integration/booking_auth.py` (new — includes `pinned_duration_error`)
- `@calendar_integration/booking_views.py` (new — mixin only)
- `@calendar_integration/booking_urls.py` (new)
- `@calendar_integration/migrations/00XX_calendarmanagementtoken_minted_by_membership_and_duration.py` (new)
- [models.py](../calendar_integration/models.py#L2004) — `minted_by_membership`, `duration`, `calmgmttoken_org_minter_idx`
- [calendar_permission_service.py](../calendar_integration/services/calendar_permission_service.py#L707) — `minted_by_user` + `duration` parameters; pin enforcement in [`can_perform_scheduling`](../calendar_integration/services/calendar_permission_service.py#L400) and [`can_perform_update`](../calendar_integration/services/calendar_permission_service.py#L379); possibly a times-aware [`can_perform_group_scheduling`](../calendar_integration/services/calendar_permission_service.py#L444) overload
- [vinta_schedule_api/urls.py](../vinta_schedule_api/urls.py#L74) — `public/booking/` include
- `@calendar_integration/tests/test_booking_code_auth.py` (new)
- `@calendar_integration/tests/services/test_calendar_permission_service_duration.py` (new)
- `@calendar_integration/tests/test_booking_code_duration_cross_surface.py` (new)
- `@calendar_integration/tests/test_management_token_minter_fk.py` (new)
- [test_calendar_permission_service_codes.py](../calendar_integration/tests/test_calendar_permission_service_codes.py) — unchanged-behavior case

**Phase 1 — code-gated single-calendar booking**

- [booking_views.py](../calendar_integration/booking_views.py) — `BookingCodeCalendarEventViewSet`
- [serializers.py](../calendar_integration/serializers.py) — `BookingCodeEventCreateSerializer`
- [booking_urls.py](../calendar_integration/booking_urls.py) — `calendar-events`
- `schema.yml` (regenerated)
- `@calendar_integration/tests/test_booking_rest_create_event.py` (new)
- [test_event_creation_surfaces.py](../calendar_integration/tests/test_event_creation_surfaces.py) — new surface

**Phase 2 — code-gated group booking**

- [booking_views.py](../calendar_integration/booking_views.py) — `BookingCodeGroupEventViewSet`
- [serializers.py](../calendar_integration/serializers.py) — `BookingCodeGroupEventCreateSerializer`
- [booking_urls.py](../calendar_integration/booking_urls.py) — `calendar-groups/<int:group_id>/events`
- `schema.yml` (regenerated)
- `@calendar_integration/tests/test_booking_rest_create_group_event.py` (new)

**Phase 3 — codeless public group booking**

- [booking_views.py](../calendar_integration/booking_views.py) — codeless branch in `BookingCodeGroupEventViewSet.create`
- `schema.yml` (regenerated)
- `@calendar_integration/tests/test_booking_rest_codeless_group.py` (new)
- [test_event_creation_surfaces.py](../calendar_integration/tests/test_event_creation_surfaces.py) — codeless surface

**Phase 3b — opaque public slug**

- [models.py](../calendar_integration/models.py) — `CalendarGroup.public_booking_slug`
- `@calendar_integration/migrations/00XX_calendargroup_public_booking_slug.py` (new)
- [booking_urls.py](../calendar_integration/booking_urls.py) — slug route
- [booking_views.py](../calendar_integration/booking_views.py) — resolve by slug on both branches
- [serializers.py](../calendar_integration/serializers.py) — read-only slug on `CalendarGroupSerializer`
- [test_booking_rest_codeless_group.py](../calendar_integration/tests/test_booking_rest_codeless_group.py) — re-pointed + enumeration-resistance
- `schema.yml` (regenerated)

**Phase 4 — code-gated reschedule and cancel**

- [booking_views.py](../calendar_integration/booking_views.py) — three actions
- [serializers.py](../calendar_integration/serializers.py) — `BookingCodeRescheduleSerializer`
- [booking_urls.py](../calendar_integration/booking_urls.py) — `events/reschedule`, `group-events/reschedule`, `events/cancel`
- `schema.yml` (regenerated)
- `@calendar_integration/tests/test_booking_rest_reschedule.py` (new)
- `@calendar_integration/tests/test_booking_rest_cancel.py` (new)

**Phase 5 — code-gated reads**

- [booking_views.py](../calendar_integration/booking_views.py) — six read actions
- [booking_urls.py](../calendar_integration/booking_urls.py) — six routes
- `schema.yml` (regenerated)
- `@calendar_integration/tests/test_booking_rest_reads.py` (new)

**Phase 6 — minting and revocation**

- [views.py](../calendar_integration/views.py) — `BookingCodeViewSet`
- [serializers.py](../calendar_integration/serializers.py) — `BookingCodeCreateSerializer`, `BookingCodeCreateResultSerializer`
- [permissions.py](../calendar_integration/permissions.py) — `BookingCodePermission`
- [routes.py](../calendar_integration/routes.py) — `booking-codes`
- `schema.yml` (regenerated)
- `@calendar_integration/tests/test_booking_code_rest_mint.py` (new)
